# ADR-012: GitHub Actions deployment workflow with OIDC + tag-trigger + manual approval

- Status: Proposed
- Date: 2026-06-12

## Context

CI is healthy (`.github/workflows/dev.yml` runs tests/ruff/mypy on every PR).
CD does not exist. Every production deploy in
[PROD-DEPLOY-LOG](../../PROD-DEPLOY-LOG.md) is run by hand:

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
```

…from one operator's laptop, with no audit trail, no smoke test, and a
separate `backup config push --stage prod` step that an operator can
forget or run out of order with the code deploy.

## Decision

Add `.github/workflows/deploy-prod.yml`:

- **Trigger:** push of a `v*.*.*` tag on `main` (and `workflow_dispatch`
  for ad-hoc redeploys of a chosen tag).
- **Auth:** OIDC role assumption — no long-lived AWS keys in GitHub
  secrets.
- **Approval gate:** GitHub environment `production` with a required
  reviewer rule.
- **Job 1 — `deploy`** (after approval): checkout the tagged commit,
  `uv sync`, `npm ci`, configure AWS via OIDC, `npx sls deploy --stage prod`,
  then `uv run backup config push --stage prod` so SSM config matches
  the deployed code in one atomic run.
- **Job 2 — `smoke-test`**: fire `health_report` async via
  `lambda invoke`, poll the Lambda log group for the Slack/SNS delivery
  markers, fail if either is missing or a `Traceback` appears.
- **Slack notification** to the ops channel on success and failure
  (existing webhook).

### Why tag-trigger

Tag-trigger ties production deploys to **explicit semver releases**.
Each `v*.*.*` tag is a deliberate "this changeset is going to prod"
decision.

### Why OIDC

GitHub OIDC has been the AWS-recommended pattern for years. If the
repo is compromised, no usable AWS credential exists outside AWS to
steal — the attacker would have to modify the workflow file, which
shows in the PR diff.

### Why a manual approval gate

A wrong tag (typo, accidental re-tag, force-push) shouldn't auto-deploy.
Adds ~30s; same gate covers `workflow_dispatch` invocations.

### Why bundle the smoke test

PROD-DEPLOY-LOG Step 17 records the cost of *not* having one: the daily
health-report fire detected an IAM regression ~12h after the deploy. A
6-min smoke test catches Slack/SNS delivery failure, IAM regressions
in the head_object loop, and config-schema mismatches in-window.

### Why bundle the SSM config push

Today it's a separate manual step after `sls deploy`. The two failure
modes are "code without config" and "config without code" — both have
happened. Running both from the same tagged commit eliminates the
ordering bug. Depends on issue #34 (SSM tier-downgrade silent failure)
being fixed first, otherwise the workflow masks its own config-push
failures.

## Cost impact

GitHub Actions minutes, OIDC role, Slack webhook reuse — all on
existing free quotas. Smoke-test Lambda invocation is <\$0.01/deploy.
**Net ongoing: \$0/month.**

## Consequences

Positive:
- Bus factor goes from 1 to anyone with repo write access + approval.
- Audit trail is automatic via GitHub Actions logs.
- Smoke test catches deploy regressions in-window.
- Code and SSM config always match (tagged-commit is the source of
  truth for both).
- Rollback is one-click via `workflow_dispatch` against a previous tag.

Negative:
- First migration deploy must reach parity with the current manual
  state (IAM, env vars, plugin versions). Validate against a non-prod
  stage first.
- Approval gate adds ~30s — real friction in a "fix prod now"
  scenario, but acceptable given urgent fixes are rare.
- OIDC provider + IAM role need a one-off setup in the backup account.

## Alternatives considered

- **Status quo** — rejected, source of the problems above.
- **Main-merge auto-deploy** — rejected; cadence is a few deploys/month,
  tag = release = deploy maps to that better.
- **Stored AWS keys in GitHub secrets** — rejected on leakage risk.
- **CodePipeline/CodeBuild** — rejected; duplicates secret management
  and observability against an already-healthy GitHub Actions setup.
- **Self-hosted runner** — rejected; OIDC removes the credential-leak
  motivation and a runner adds ongoing maintenance.
- **Mike docs deploy in the same workflow** — out of scope; separate
  `docs-deploy.yml` follow-up.

## Risks

- **OIDC role permissions over-broad.** `sls deploy` needs
  CloudFormation + Lambda + IAM + EventBridge + SNS + S3 + Logs.
  Mitigation: scope to resources matching `nzshm-backup-*` where the
  API supports resource-level ARNs.
- **Tag-trigger fires on any `v*.*.*` tag.** Mitigation: workflow
  guards on `github.ref` and the tagged commit's branch.
- **Smoke-test window false negatives** if delivery happens to take
  >6 min. Empirical baseline is 4-5 min; extend window if needed.
- **Approval-gate self-approval** is GitHub-blocked by default, which
  matters in solo-operator periods. Mitigation: configure the rule to
  permit self-approval, or document an emergency manual-deploy
  fallback.

## Implementation scope

| Component | Effort |
|---|---|
| OIDC provider + IAM role in backup account | ~30 min (one-off) |
| `production` environment with approval rule | ~5 min (repo settings) |
| `.github/workflows/deploy-prod.yml` | ~90 lines |
| Update `docs/development/release.md` | ~10 lines |
| New `docs/development/deploy-workflow.md` operator handbook | ~80 lines |
| Validate against non-prod stage before pointing at prod | ~30 min |

Rollout: workflow lands deploying to staging first, then flipped to
`--stage prod` once parity is confirmed.

## Out of scope

- Mike docs deploy (separate workflow).
- Pre-deploy schema migrations (Pydantic `extra="ignore"` covers it).
- Multi-region deploy (only `ap-southeast-2` today).
- Per-source canary deploys (daily health report's weka canary already
  covers this).

## Links

- [docs/development/release.md](../../development/release.md) — current
  manual release process; updated after this lands.
- [PROD-DEPLOY-LOG](../../PROD-DEPLOY-LOG.md) — historical deploys;
  Steps 14, 17, 18, 19, 21 motivated this ADR.
- Issue #34 — SSM tier-downgrade silent failure (must be fixed before
  this workflow's SSM-push step lands).
- AWS docs: [Configuring OpenID Connect in Amazon Web Services](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
