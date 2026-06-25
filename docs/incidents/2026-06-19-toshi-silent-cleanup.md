# 2026-06-19 — toshi silent UNLOAD-cleanup failure

> **Status:** Resolved.
> **Tracking issues:** #40 (root cause + fix); follow-ups #41 (alerting gap), #42 (dependency audit), #46 (docs publishing question).
> **Fix:** PR #43, deployed 2026-06-22.

## Summary

A silent `AccessDenied` on `s3:DeleteObject` against the inventory
bucket's Athena UNLOAD working prefix left a stale 369-byte part
file. The next UNLOAD failed at submission with
`HIVE_PATH_ALREADY_EXISTS`. The backup handler caught the failure,
logged it, and returned `statusCode 500` — which the Lambda
runtime treats as a successful invocation. The existing
`AWS/Lambda Errors` alarm therefore did not fire. Detection waited
for the next morning's daily health-report `count_delta` check.

## Impact

- toshi S3 sync blocked for ~3 days (2026-06-18 → 2026-06-22).
- Two source keys missing from the backup. Source data intact, so
  no permanent data loss risk; impact was elevated point-in-time-
  recovery window for those specific keys.
- toshi DynamoDB exports unaffected (separate pipeline).
- No other source affected (ths / static / weka all had
  `row_count = 0` during the window — never exercised the failing
  cleanup path).
- Daily reports turned RED on 2026-06-19; auto-healed visually on
  2026-06-21 as the inventory snapshot caught up, despite the
  underlying UNLOAD bug remaining.

## Timeline

| NZST | UTC | Event |
|---|---|---|
| 2026-06-17 09:46 | 2026-06-16 21:46 | First non-empty toshi UNLOAD in recent window: `row_count=5`. Manifest concatenated to backup bucket, S3 Batch copy succeeded. Post-UNLOAD `_cleanup_unload_parts` silently fails on `AccessDenied`. 369-byte part file left behind. |
| 2026-06-18 09:46 | 2026-06-17 21:46 | Next scheduled toshi backup. Pre-UNLOAD cleanup again silently swallows AccessDenied. UNLOAD then fails at submission with `HIVE_PATH_ALREADY_EXISTS`. Handler logs `[ERROR] Backup failed for nzshm22-toshi-api-prod: …`, returns `statusCode 500`. Alarm does not fire. |
| 2026-06-19 ~17:00 | ~05:00 | Daily health report fires RED: "toshi: backup is missing 2 source keys". First human-visible signal. |
| 2026-06-20, 2026-06-21 | — | Reports continue RED then GREEN as inventory snapshot catches up to the "auto-healed since snapshot" steady state. Underlying UNLOAD remains broken. |
| 2026-06-22 ~10:00 | ~22:00 prior day | Operator opens triage. CloudWatch log inspection shows succeeded-then-failed pattern across the two morning runs. |
| 2026-06-22 ~12:00 | — | Root cause identified — missing IAM permission + silent error in `_cleanup_unload_parts`. PR #43 opened. |
| 2026-06-22 ~14:00 | — | PR #43 merged, stack redeployed. Stale part file removed via `aws s3 rm`. Manual `backup run --source toshi` recovers the missing keys. |
| 2026-06-22 end of day | — | Follow-up issues #41 (alerting), #42 (dependency audit) filed. #46 (mike docs) filed shortly after. |

Detection-to-fix elapsed: ~3 days from first failure to fix
deployed. First-failure-to-first-human-signal: ~24 h.

## Root cause

Two concurrent gaps; either alone would not have caused this
specific incident.

### 1. Missing IAM permission

The Lambda execution role had `s3:DeleteObject` scoped to
restore-test temp buckets only:

```
"Resource": [
  "arn:aws:s3:::bb-restore-test-*",
  "arn:aws:s3:::bb-restore-test-*/*"
]
```

The inventory bucket `nzshm-backup-inventory-…` was not in scope.
This was a permission gap from the original deployment that never
surfaced because the Athena UNLOAD flow had only ever exercised
cleanup against an empty prefix.

### 2. Silent error in `_cleanup_unload_parts`

`src/nzshm_backup/athena_inventory.py:455-469` (pre-#43) called
`delete_objects` with `Quiet: True` and never inspected the
returned `Errors` array. From boto3's perspective the call
succeeded — no exception thrown — and the per-key `AccessDenied`
was silently dropped.

## Why this hadn't surfaced earlier

The cleanup path is only meaningful when `row_count > 0` — i.e.
when an actual non-empty UNLOAD result needs cleaning up. Every
UNLOAD in the recent window before 2026-06-17 had
`row_count = 0`; the cleanup function was being called but doing
nothing (early-return on empty list).

2026-06-17's toshi UNLOAD was the **first non-empty UNLOAD in
recent history**, the first invocation that actually exercised
the delete path. The latent IAM gap finally bit.

## Why detection lagged

The existing alarm `nzshm-backup-lambda-errors-prod` watches
`AWS/Lambda Errors`, which only ticks for *uncaught* Lambda
exceptions (handler raise, init failure, OOM, timeout). The
backup handler catches per-source failures, records them in
`result.errors`, and returns `statusCode 500`. The Lambda runtime
treats that as a successful invocation; the metric stays at zero.

The `logger.error("Backup failed for …")` line was present in
CloudWatch Logs but nothing was watching the log group.

Detection therefore waited for the slow signal: the daily
health-report `count_delta` between source and backup inventories.

## Resolution

PR #43, deployed 2026-06-22:

- **IAM**: scoped `s3:DeleteObject` added on
  `arn:aws:s3:::nzshm-backup-inventory-*/_manifests/unload/*` —
  narrowly scoped, preserves the no-delete guarantee on backup
  buckets.
- **Code**: `_cleanup_unload_parts` now inspects the `Errors`
  array on the `delete_objects` response and raises `RuntimeError`
  on any per-key failure. Regression test added.

Plus two manual operations the deploy alone couldn't do:

- `aws s3 rm …/_manifests/unload/toshi/…` — remove the stale
  part file, which pre-existed the deploy.
- `backup run --source toshi` post-deploy to pick up the two
  missing keys.

## Lessons

1. **`Quiet: True` on `delete_objects` is a sharp tool.** Hides
   per-key errors by default; needs explicit `Errors` inspection
   to be safe. Any new use of `Quiet: True` should be reviewed
   on that basis.

2. **IAM scope review when deploy-time behaviour changes.** The
   inventory-bucket scope was correct for the system as it was
   originally designed (no cleanup needed against the control
   bucket). The Athena UNLOAD pipeline added the need for
   cleanup; the IAM didn't get updated alongside. A "what
   permissions does the new code path need?" question on PR
   review for any change that adds a new boto3 call would catch
   this class.

3. **`AWS/Lambda Errors` is necessary but not sufficient.**
   It alarms on Lambda crashing; it does not alarm on Lambda
   *failing its work* but returning cleanly. For handlers that
   catch-and-return, an additional log-metric-filter alarm on
   `[ERROR]` log lines (or similar) is needed. Tracked separately
   as #41.

4. **Detection latency was driven by the failure mode, not by a
   broken observability stack.** The backup engine logged the
   error correctly. The failure was that nothing was watching the
   log group. This is distinct from "the backup engine silently
   corrupted data" type incidents; it's "the backup engine
   cleanly aborted but no one was watching".

## Follow-up issues

| Issue | Triggered by | Outcome |
|---|---|---|
| #41 — alerting gap on caught-and-logged failures | Lesson 3 above | Resolved by PR #47 — log-metric-filter alarm + pitr-watcher alarm + SNS→Slack alarm-bridge |
| #42 — dependency audit triggered by review of #40 patch surface | 30 open Dependabot alerts noticed during the #40 deploy | Resolved by PR #45 — Python (urllib3, cryptography, idna, pymdown-extensions, uv) and npm (axios, form-data) upgrades |
| #46 — versioned docs publishing via mike | Surfaced during the post-incident docs walk; mike is referenced in `mkdocs.yml` but isn't a declared dep and `gh-pages` doesn't exist | Open — deferred pending OSS migration |

## Forward link

The #41 alerting-gap discussion fed into a broader strategic
review of the project's operational posture, which led to the
dormant `docs/design/open-source-migration.md` being promoted to
an active workstream. The SAM-cutover arc and the OSS-migration
arc were carried in parallel from there. See
[SAM_MIGRATION_LOG](../SAM_MIGRATION_LOG.md) for the migration
narrative.
