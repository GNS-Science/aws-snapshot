# Project Status — 30 March 2026

## Phase progress

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | CLI + config + S3 backup | ✅ Complete |
| 2 | DynamoDB PITR, S3 Batch, scheduling, Lambda | ✅ Complete |
| 3 | Notifications, cost tracking, compliance reports | ⏳ Not started |
| 4 | Restore (S3 + DynamoDB, cross-account) | ✅ Substantially complete |
| 5 | Testing, validation, event audit log | ✅ Core done |
| 6 | Parallel run, NSHM production cutover | 🔄 Arkivalist done; NSHM pending |

## What's working (arkivalist account)

- Hourly scheduled S3 backups via EventBridge → Lambda
- DynamoDB PITR exports triggered on schedule
- Full backup → restore → validate cycle tested end-to-end
- Event audit log (JSONL in `_events/`) recording `backup_run`, `backup_run_complete`,
  `restore_submitted`, `restore_completed`, `pitr_reenabled`, `test_restore`
- `backup test integrity` — S3 ETag diff + DynamoDB PITR check (paginated export count)
- `backup test restore` — direct-copy and S3 Batch Operations paths, with ETag verification
- `backup restore run` — S3 (direct + Batch) and DynamoDB PITR restore with async status tracking
- `backup events` — shows event audit log with localised timestamps
- `backup schedule show` — EventBridge rules with localised run-time descriptions
- All mutating commands support `--dry-run`
- Localised datetime input/output across CLI (NZDT/NZST/AEST/AEDT)

## Blockers for NSHM production (461564345538)

1. **Cross-account IAM roles not configured** — run `scripts/create-source-roles.py` against account 461564345538
2. **Parallel run not started** — still on AWS Backup for NSHM datasets
3. **No failure alerting** — Phase 3 minimum required before production cutover

## Minor known gaps

- DynamoDB restore fallback when PITR window >35 days (need export-based restore path)
- Automated `test restore` scheduling via EventBridge (currently manual only)
- `test full-drill` not yet implemented (planned quarterly DR drill)
- No budget alerts or compliance export (Phase 3)

## Security posture

**Strong:**
- Reader/restore IAM role split — Lambda backup role cannot restore; restore role cannot backup
- `sts:ExternalId` on all cross-account assume-role calls (confused-deputy protection)
- Runtime bucket policy injection for temporary restore-test buckets
- S3 versioning enabled on all backup buckets
- No cross-account write path in the backup direction

**Acceptable risks (documented in `docs/design/iam-security-decisions.md`):**
- Restore role uses broad `dynamodb:*` — required by undocumented AWS PITR internals
- S3 Batch role uses `bb-*` wildcard — source bucket policies are the real access gate

**Not yet addressed:**
- No failure alerting (SNS/Slack webhook)
- No budget alerts
- No compliance export

## Estimated cost trajectory

| Item | Before | After |
|------|--------|-------|
| AWS Backup (NSHM) | ~$1,700 NZD/month | $0 (after cutover) |
| Custom solution | $0 | ~$29 NZD/month |
| **Annual saving** | | **~$20,000 NZD/year** |

## Suggested next actions

1. `git push` + tag a release (multiple commits ahead of origin)
2. Configure NSHM cross-account IAM: `python scripts/create-source-roles.py` against account 461564345538
3. Phase 3 minimum: Slack/SNS failure alerting before NSHM production cutover
4. Set a concrete parallel run start date and AWS Backup deprecation timeline
