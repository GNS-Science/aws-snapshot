# ADR-008: Notification recipients managed from YAML, not CloudFormation

- Status: Implemented
- Date: 2026-05-22

> **2026-06-27 update — extended by
> [ADR-013](ADR-013-discord-notification-support.md).** ADR-008 set
> the YAML-driven recipients/config pattern for Slack + SES. ADR-013
> adds Discord as a peer chat channel under the same model: a
> `notifications.discord` block in the production YAML, secret name
> for the webhook URL, independent enable flag. The recipients-from-
> YAML invariant is unchanged.

## Context

PR #20 (ADR-005 fast path, 2026-05-19) and PR #21 (ADR-005 slow path,
2026-05-20) shipped an asymmetric notification design:

- The first email recipient per channel lived in
  `backup-config.production.yaml` under `notifications.alerts.email` /
  `notifications.reports.email.address`.
- That recipient was wired to an SNS subscription declaratively in
  `serverless.yml` via `AWS::SNS::Subscription` resources gated by
  CloudFormation conditions (`HasAlertEmail`, `HasReportEmail`).
- Additional recipients had to be added via raw `aws sns subscribe`
  CLI calls, which:
  - Don't live in the repo.
  - Aren't visible in `backup-config.yaml`.
  - Are silently preserved through `sls deploy` but lost on stack
    teardown/rebuild.

Operator feedback (2026-05-22):

> "it's a bit confusing adding 1st entry via yaml and others via aws cli."

The split was a real wart: contributors had two places to look,
two workflows to remember, and the source of truth was effectively
split between the repo and AWS state.

## Decision

Make `backup-config.{stage}.yaml` the **single source of truth** for
both topics' recipient lists. Move subscription management out of
CloudFormation entirely. Add a `backup notifications apply` CLI command
that reconciles each topic's actual SNS subscriptions to match the YAML
lists.

### Schema change

```yaml
notifications:
  alerts:
    emails:                              # was: email: str | None
      - oncall@example.com
  reports:
    email:
      enabled: true
      addresses:                         # was: address: str | None
        - reports-list@example.com
```

### serverless.yml change

Dropped:

- `BackupAlertEmailSubscription`, `BackupReportEmailSubscription`
  (per-config-recipient `AWS::SNS::Subscription` resources)
- `HasAlertEmail`, `HasReportEmail` conditions
- `custom.alertEmail`, `custom.reportEmail`, `custom.reportEmailEnabled`
  variable resolvers

Kept (CloudFormation continues to own these):

- `BackupAlertsTopic`, `BackupReportsTopic` (`AWS::SNS::Topic`)
- `BackupLambdaErrorAlarm` (`AWS::CloudWatch::Alarm`)
- All IAM (Lambda role `sns:Publish` to reports topic)

### New CLI

`src/aws_snapshot/commands/notifications.py`:

- `backup notifications apply [--stage prod] [--dry-run] [--only alerts|reports|all]`
  - Reads desired lists from config.
  - Lists actual SNS subscriptions on each topic.
  - Subscribes new addresses, unsubscribes stale ones.
  - Leaves pending confirmations alone (AWS cannot reissue them;
    expire after ~3 days; a subsequent apply re-subscribes if still
    listed).
  - Case-insensitive email comparison.
  - Ignores non-email protocols on the topic (e.g. SQS, HTTP).
- `backup notifications show [--stage prod]` — read-only listing of
  every email subscription on both topics with `confirmed` / `pending`
  state.

### Operator workflow

```bash
# Edit list
$EDITOR backup-config.production.yaml

# Apply (preview first if large)
uv run backup notifications apply --dry-run
uv run backup notifications apply

# Verify
uv run backup notifications show
```

No `sls deploy` required for recipient changes. The Lambda role + the
topics themselves are still CFN-owned, so deploys still re-create them
correctly if the stack is ever torn down.

## Alternatives considered

### Option A — Keep YAML as single source, expand list to N `AWS::SNS::Subscription` resources in CFN

Cleanest declarative model. Requires either:

- A Serverless Framework plugin that iterates a list into N resource
  blocks, or
- A custom CloudFormation macro / nested stack to template the
  resources.

Rejected because: CFN doesn't natively iterate, and pulling in a
plugin or macro adds a moving part to the deploy pipeline for what is
fundamentally a small set of email addresses. Also: CFN-managed
subscriptions would mean every recipient change requires a
`sls deploy`, which is a longer round-trip than `notifications apply`.

### Option B — YAML lists, CLI converger (chosen)

- ✅ Single source of truth (YAML).
- ✅ No CFN plumbing for individual subscriptions.
- ✅ Recipient changes don't require deploy.
- ✅ Idempotent by construction; dry-run available.
- ✅ Easy to test (pure diff function).
- ⚠ Needs the operator to remember to run `apply` after editing YAML
  (same friction as `backup config push`).

### Option C — Single mailing-list address per channel

Point the existing singular config entry at a Google Group / corporate
mail alias. Individual membership lives at the mail provider; the
backup system sees one subscription per topic.

- ✅ Zero code change.
- ✅ Clean separation of concerns (backup system doesn't manage your
  contact list).
- ✅ Common ops pattern.
- ❌ Requires a mailing list to exist as an external dependency.
- ❌ Couples on-call membership management to the IT mail-alias process.

Rejected for this team because GNS Science prefers zero external admin
dependencies — keeping recipient management in the same repo as the
code is operationally simpler for a small team.

### Option D — Drop singular field, require operator to subscribe manually post-deploy

Honest about the imperative nature: YAML has no recipient field;
operator runs `aws sns subscribe ...` after each deploy.

- ✅ Simplest code.
- ❌ Loses the "deploy from scratch and it works" property.
- ❌ Every new operator account / disaster recovery rebuild needs the
  manual step or notifications silently don't work.

Rejected as a regression from the existing CFN-managed behaviour.

## Consequences

- **Operationally:** recipient changes are now a 2-command workflow
  (`edit YAML, apply`) — no deploy, no AWS console, no `aws-cli` muscle
  memory. Membership is reviewable in git history.
- **Implementation:** ~200 lines (`notifications.py` + 7 unit tests).
  Migration was a single `apply` after the CFN cleanup deploy on
  2026-05-22.
- **Risk:** an operator who forgets to run `apply` after editing YAML
  has a divergence between repo and AWS state. Mitigation: documented
  prominently in `docs/operations/cheatsheet.md`. Future improvement
  could be a CI check that diffs config against live SNS state and
  warns on drift.
- **Future direction:** the same pattern could replace any other
  imperative `aws-cli` ops the system accumulates (e.g. ad-hoc IAM
  policy attachments). Currently scoped to SNS subscriptions only.

## Links

- `src/aws_snapshot/commands/notifications.py` — implementation
- `tests/test_notifications_apply.py` — diff unit tests
- `docs/operations/enabling-notifications.md` — operator runbook
  rewritten for this design
- `docs/operations/cheatsheet.md` — quick reference for the new
  workflow
- ADR-005 (revised) — parent design for the two notification channels
