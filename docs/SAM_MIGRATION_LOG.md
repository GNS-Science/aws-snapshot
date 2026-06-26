# SAM migration log

Narrative record of the deploy-tool migration from Serverless
Framework v4 to AWS SAM, plus the surrounding OSS-migration work
(shim-repo decision, PyPI placeholder, docs scrub) that was
progressed alongside.

Companion design doc:
[`docs/design/open-source-migration.md`](design/open-source-migration.md).
Per-PR descriptions and `nzshm-backup-ops/docs/PROD-DEPLOY-LOG.md`
(shim copy) carry the granular detail this log abstracts over.

---

## 2026-06-22 to 2026-06-24 — SAM cutover arc

### Trigger

Three threads converged to promote the dormant
`docs/design/open-source-migration.md` to an active workstream:

1. The post-#40 review of the existing alarm coverage (tracked
   under #41 — see
   [`incidents/2026-06-19-toshi-silent-cleanup.md`](incidents/2026-06-19-toshi-silent-cleanup.md))
   surfaced that the project's operational posture had drifted from
   the original ADR-005 design.
2. The #46 mike-docs-publishing question made explicit that any
   public docs deploy would need a hygiene pass over GNS/NSHM-
   internal content — i.e. Section 3 of the migration plan would
   need to land before docs could publish.
3. Recent Serverless Framework v4 SaaS-pivot pressure (mandatory
   org-account login) gave a freshly-concrete reason to do the
   deploy-tool replacement that the migration plan had drafted but
   never executed.

Tracked under **#48** (workstream issue).

### Two architectural decisions amended into the migration doc (PR #49, merged 2026-06-24)

1. **Shim-repo strategy (new §3a).** Chose a private
   `gns-science/nzshm-backup-ops` shim — depending on the OSS
   engine as a PyPI dep — over a private fork carrying GNS-only
   files. Rationale: forks accumulate a merge-conflict tax over
   time and mix operational secrets with engine code; shims keep
   the engine generic and force OSS-friendly abstractions at the
   dependency boundary. Boundary rule: *anything that is a
   behaviour change goes upstream; the shim only carries
   configuration and operational state*.

2. **AWS SAM over the originally-proposed boto3 deploy script
   (§2 rewrite).** Original draft picked a boto3 script over
   Serverless Framework, CDK, and downgraded sls v3 with "zero
   extra tools" as the deciding criterion — but SAM was absent
   from the comparison. SAM's no-bootstrap-stack requirement (vs
   CDK), AWS-first-party status (vs Serverless Framework's pivot
   to SaaS), and "declaration not orchestration" footprint (vs
   the 300-500 lines of orchestration a boto3 script would carry
   forever) made it the right choice.

### Workstream PRs

| PR | Title | Status |
|---|---|---|
| **#49** | Migration doc amendment: shim strategy + SAM choice | Merged 2026-06-24 |
| **#50** | Identifier scrub — AWS account IDs + GNS access-control identifiers (17 files, 101 substitutions) | Open (ready for review) |
| **#51** | AWS SAM template + verification + cutover runbooks | Merged 2026-06-24 |
| **#52** | Brand-string scrub — `nzshm-backup` (project noun) and `nzshm_backup` (Python module path) in docs (17 files, 64 substitutions); preserves deployed-resource references | Open (ready for review) |
| **#53** | Package rename `nzshm-backup` → `aws-snapshot` | Open (ready for review — lands after `serverless.yml` removal) |

### PyPI placeholder claim

**2026-06-23.** Reserved `aws-snapshot` on PyPI with a 4-file
placeholder package at version 0.0.0
([pypi.org/project/aws-snapshot](https://pypi.org/project/aws-snapshot/)).
Scaffold lived in a throwaway sibling folder and was uploaded
under a personal PyPI account with 2FA. To be moved to a
GNS-Science PyPI organisation if and when one exists.

### `nzshm-backup-ops` shim repo scaffolded (local-only)

**2026-06-23.** `/Users/Shared/DEV/GNS/MISC/nzshm-backup-ops/`
initialised with README, `pyproject.toml` placeholder
(`aws-snapshot` dep commented out until first PyPI publish),
`.gitignore`. Two commits to date:

1. Scaffold + boundary-rule README.
2. `docs/PROD-DEPLOY-LOG.md` copied across from the public repo
   with a dual-location banner; both copies live in parallel
   until the cutover-safety rule is satisfied.

No GitHub remote yet — destination is `gns-science/nzshm-backup-ops`
(private), creation deferred until ready to onboard a second
operator.

### PR #51 — scope evolution

The largest PR in the workstream went through three scope changes
as understanding sharpened:

1. **Initial:** SAM template + verification runbook only.
2. **After side-stack verification:** added six template fixes
   (see below) + cutover runbook.
3. **Pre-merge:** package rename split out into #53. Reviewers
   asked the rename and SAM cutover be reviewable as independent
   operational decisions, so PR #51 was rebased to remove the
   rename and the SAM template handler paths reverted to
   `nzshm_backup.*`.

Six template fixes the verification exposed — each one a real
gap that wouldn't have surfaced without a deploy-against-AWS
exercise:

| Fix | Why it was needed |
|---|---|
| `BuildMethod: makefile` | SAM's default Python builder packages the entire CodeUri verbatim — `.venv` (381 MB), `docs/`, `tests/`, AND `backup-config.production.yaml` would have shipped in the Lambda artefact. `.aws-samignore` is documented but ignored by `PythonPipBuilder`. |
| `AllowedPattern` instead of `AllowedValues` on `Stage` | Original `[prod, sandbox]` rejected `Stage=test` for the side-stack verification. |
| Stage-suffixed pitr-watcher rule | Hardcoded `nzshm-backup-pitr-watcher` collided with the live sls rule during side-stack verification. Stage suffix means the SAM rule is `nzshm-backup-pitr-watcher-prod` after cutover; the env var the watcher reads to find its own rule is also Stage-scoped. |
| Explicit `BackupLogGroup` + `LoggingConfig` + `!Ref BackupLogGroup` in MetricFilter | Lambda creates log groups lazily on first invocation; the MetricFilter creation happens before any invocation, so CFN failed with "log group does not exist". |
| Robust `__init__.py` fallback for missing `_version.py` | `_version.py` is hatch-vcs-generated at wheel-build time, absent in plain src copies. The SAM Makefile build copies `src/aws_snapshot/` directly without going through the wheel build. |
| Single-line `parameter_overrides` in `samconfig.example.toml` | TOML triple-quoted strings preserve embedded newlines; SAM passes them through with `\n` characters in each value; CloudFormation rejects them with "Parameter 'Stage' must be one of AllowedValues" — misleading error. |

### Activity A — side-stack verification (2026-06-23 ~15:00 NZST)

Deployed SAM stack `nzshm-backup-sam-test` (`Stage=test`) in the
prod backup account, parallel to the live sls
`nzshm-backup-service-prod`. Same physical account, isolated
resource names (`-test` suffix everywhere) so no collision.

All 15 SAM resources `CREATE_COMPLETE`; backup Lambda cold-start
succeeded (`statusCode 500` from the expected SSM-config-not-found
path for Stage=test, the desired empirical confirmation that the
handler ran end-to-end); alarm-bridge → Slack delivered
(`delivered=1`); IAM parity diff against the sls role clean modulo
cosmetic string-vs-array shape differences; env-var parity diff
clean modulo expected stage suffixes. Stack torn down via
`sam delete` post-verification.

The verification proved the SAM template's structural correctness
*regardless of package name* — the handler path is a string in
the template; whether it says `nzshm_backup.*` or `aws_snapshot.*`
has no bearing on build/deploy/runtime behaviour.

### Activity B — production cutover (2026-06-24 15:23 → 16:25 NZST)

Real cutover from the sls stack to the SAM stack of the same name
(`nzshm-backup-service-prod`). Followed the runbook
([`docs/development/sam-cutover-runbook.md`](development/sam-cutover-runbook.md))
step by step:

| Step | NZST | Outcome |
|---|---|---|
| 2 — Disable pitr-watcher rule | 15:23 | Already DISABLED — no in-flight restore |
| 3 — `sls remove --stage prod` | 15:26 → 15:29 | sls stack gone; **no-Lambda window opens** |
| 4 — `make sam-build` + `sam deploy` | ~3 min in CFN | SAM stack created; **no-Lambda window closes** |
| 4 sub-check | All 15 resources `CREATE_COMPLETE` | |
| 5 — `backup notifications apply` | 16:22 | 6 subscriptions created (3 emails × 2 topics) in `PendingConfirmation` — recipients must click confirmation links before email delivery starts; Slack via alarm-bridge works immediately |
| 6 — Recreate schedules | 16:23 | All 5 schedules (4 source + health-report) removed + re-added; targets registered against new function ARN; freshly-created `Lambda::Permission` per rule |
| 7a — Dry-run invoke | 16:23 | `statusCode 200, success: true`. **First successful cross-account assume-role on the new SAM-deployed IAM role.** |
| 7b — Synthetic alarm | 16:24 | Slack received via alarm-bridge (`delivered=1`) |
| 8 — Manual `backup run --source weka` | 16:24 → 16:25 | `Backup completed successfully` in ~26 s, `row_count = 0` (steady state) |

No-Lambda window between sls remove completing and SAM stack
reaching CREATE_COMPLETE: ~3 minutes — well inside the 5-minute
estimate.

### Post-cutover proof-of-life (2026-06-24 17:15 NZST)

To exercise the EventBridge → Lambda invoke permission (the one
piece of the cutover that hadn't been tested during steps 7-8),
the weka schedule was temporarily moved from 09:45 NZST to
17:15 NZST. The 17:15 fire landed cleanly:

- Received scheduled event payload `{"source": "weka", "trigger_type": "scheduled"}`
- Athena UNLOAD ran, `row_count = 0`
- "Nothing to copy — manifest is empty, skipping job submission"
- Total duration 21.1 s
- No `[ERROR]` lines

Schedule reverted to 09:45 NZST so weka stays in lockstep with
the other three source schedules.

### voj review feedback on PR #51 (post-merge)

Three substantive comments + LGTM:

1. **`sam-deploy-verification.md:25`** — "could this be a dev
   dependency?" Re: `pip install --user aws-sam-cli` prerequisite.
   Valid. Goes in the `serverless.yml`-removal follow-up PR.
2. **`sam-deploy-verification.md:249`** — "are these all done?" Re:
   the cutover-criteria checkboxes. Yes for pre-merge boxes;
   partially yes for post-merge (cutover succeeded + PROD-DEPLOY-LOG
   recorded; first clean daily cycle still pending). Ticks land
   in the follow-up PR.
3. **`template.yaml:1`** — "is `template.yaml` the default
   filename for SAM? it feels very generic. I'd prefer something
   like `sam.yaml`." Reasonable; `samconfig.toml`'s
   `template_file = "sam.yaml"` makes the rename transparent
   after one line. Folds into the follow-up PR.

### Open follow-ups

1. **Email subscription confirmation** — 6 confirmation emails
   need clicking (3 recipients × 2 topics). Slack delivery is
   unaffected.
2. **First scheduled daily cycle** — 2026-06-25 09:45 NZST. If
   green, the cutover-safety rule's "one clean daily cycle" gate
   is satisfied.
3. **`serverless.yml`-removal PR** opens after (2) is green.
   Folds in @voj's three review comments above:
   - `aws-sam-cli` to dev dependency group
   - Rename `template.yaml` → `sam.yaml` + `samconfig.toml` line
     `template_file = "sam.yaml"`
   - Tick the post-merge cutover-criteria checkboxes
   - Delete `serverless.yml`, `package.json`, `package-lock.json`,
     `node_modules/`, the `.serverless/` ignore entry
   - Collapse `lambda-deployment.md` + `release.md` deploy
     sections to SAM-only
   - Delete public `docs/PROD-DEPLOY-LOG.md` (shim version
     becomes authoritative)
4. **PR #50, #52, #53** — independent of (3); merge any time
   after review.
5. **Other migration plan sections not yet started** — Section 5
   (config example), Section 6 (PyPI packaging + release),
   Section 9 (docs publishing via #46).

### Lessons

A handful worth recording for future operational migrations.

1. **Verify against a real stack — not just locally — before
   marking a deploy-template PR ready.** Six template fixes
   landed because Track 2 of the verification runbook exercised
   real CloudFormation. Local `sam validate` would have caught
   none of them.

2. **Runbooks are dual-use: validate the procedure, then
   preserve it as documentation.** Both the verification and
   cutover runbooks were written first as checklists for the
   author to follow; preserving them on `main` means any future
   operator can re-run them. Cheap to write at the time of doing,
   expensive to write retroactively.

3. **Bundle scope only when it reduces churn; otherwise split.**
   PR #51 originally bundled the SAM template and the package
   rename per an earlier "option C" call. On reflection the two
   concerns are independent operational decisions and the split
   made review tractable. The rebase was an hour's work — cheap
   compared to a reviewer asking "why are these together?".

4. **Cutover safety rule pays off the first time it's invoked.**
   Keeping `serverless.yml` on `main` through the cutover meant
   the rollback path was one command (`sls deploy --stage prod`).
   No rollback was needed, but the option existed throughout,
   which made the destructive steps less stressful to execute.

5. **"First cross-account assume-role" only happens for real in
   cutover step 8.** Worth flagging explicitly in cutover docs —
   verification runbooks that don't exercise cross-account give
   false confidence that a renamed IAM role will Just Work for
   AssumeRole. Source-account trust based on account principal
   (not role ARN) makes it work; but the empirical confirmation
   is step 8, not step 7a.

---

## Postscript — 2026-06-26 follow-up + samconfig trap

Two days after the cutover. Three things landed in one session:

### The remaining PR queue cleared

PRs #50 (identifier scrub), #52 (brand-string scrub), #53
(package rename `nzshm-backup` → `aws-snapshot`), and #55
(`serverless.yml` removal + @voj's PR #51 review feedback) all
merged. The repo is now genuinely OSS-shaped: no Node toolchain,
no GNS account IDs or operator identities in public docs, no
`nzshm-backup` project-name strings in user-facing prose, and
the Python package is `aws_snapshot` under the `aws-snapshot`
PyPI namespace. **The cutover-safety "one clean daily cycle"
gate landed two greens** (2026-06-25 and 2026-06-26 morning
fires), which unblocked PR #55's `serverless.yml` deletion.

### Deployed Lambda synced with the renamed package

A code-only `sam deploy` to update the deployed Lambda artefacts
from the (still-running) pre-rename `nzshm_backup` bundle to
the new `aws_snapshot` bundle. The CFN stack was structurally
unchanged — only function code hashes differed. Verified via
dry-run smoke test.

### A new lesson: samconfig `template_file` scope matters

The first redeploy attempt **failed**. Diagnostic:

- PR #55 added `template_file = "sam.yaml"` in
  `[default.global.parameters]` of `samconfig.example.toml`
  so SAM CLI would discover the renamed-from-`template.yaml`
  template file.
- *Global* parameters are honored by `sam deploy` too — not just
  `sam build`. `sam deploy`'s default is to use
  `.aws-sam/build/template.yaml`, the build-output template
  with `CodeUri` rewritten to point at each function's clean
  build directory. With `template_file` set globally,
  `sam deploy` instead used the source `sam.yaml` with
  `CodeUri: ./` — and uploaded the entire source tree as the
  Lambda artefact.
- Lambda errored at cold-start with
  `Runtime.ImportModuleError: Unable to import module 'aws_snapshot.lambda_handler': No module named 'aws_snapshot'`
  because the package lives at `src/aws_snapshot/`, not at the
  (would-be) artefact root.
- Fixed in PR #56 by moving `template_file = "sam.yaml"` to
  `[default.build.parameters]`. The redeploy after that succeeded.

This is the **first real-world fire of the #41 layered
alarms post-cutover**. Both `lambda-errors-prod` and
`lambda-log-errors-prod` tripped within ~5 minutes, posted to
Slack via the alarm-bridge, and auto-cleared once PR #56's fix
was applied. The whole observability stack worked end-to-end on
its first real test. Production impact: zero — no scheduled
fire was due in the affected window.

### Sixth lesson (added to the list above)

6. **Even after one successful real-stack deploy, a config
   change between deploys can re-introduce trap conditions.**
   The original cutover (Activity B) deployed cleanly because
   `template_file` was at its default; PR #55 changed that
   default in pursuit of a renamed template file (@voj's
   review feedback) and the change wasn't re-validated against
   a real deploy. A general principle: changes to deploy
   *configuration* (samconfig.toml shape, IAM policy scope,
   build cache settings) deserve the same "verify against a
   real stack before merging" gate as changes to the deploy
   *template* itself.

---

*Maintained as a session record. New migration phases — PyPI
publish, public docs deploy, additional cutovers — get their own
dated sections appended here.*
