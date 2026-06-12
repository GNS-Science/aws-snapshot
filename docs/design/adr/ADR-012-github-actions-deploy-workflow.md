# ADR-012: GitHub Actions deployment workflow with OIDC + tag-trigger + manual approval

- Status: Proposed
- Date: 2026-06-12

## Context

The CI side of the pipeline is healthy — `.github/workflows/dev.yml`
runs tests, ruff, and mypy on every PR via the shared
`GNS-Science/nshm-github-actions/python-run-tests-uv` workflow.

The **CD side does not exist**. Every production Lambda deploy in
[PROD-DEPLOY-LOG](../../PROD-DEPLOY-LOG.md) (at least nine of them
between 2026-04 and 2026-06) used the same incantation:

```bash
AWS_PROFILE=nshm-backup-admin npx sls deploy --stage prod
```

…run manually from one operator's laptop. Real operational risks
follow:

1. **Bus factor = 1.** Only `chrisbc`'s SSO profile is configured for
   sls deploys. If they're on holiday or sick, the production system
   cannot be redeployed (so urgent fixes have to wait, or another
   operator has to bootstrap SSO + node + serverless from scratch
   under pressure).
2. **Workstation drift.** Different node versions, sls plugin
   versions, `.env` files, or partially-stashed working trees produce
   subtly different deploys. The 2026-05-22 IAM gap (PROD-DEPLOY-LOG
   Step 17) and the 2026-06-02 SSM tier-downgrade bug (issue #34)
   both ultimately surfaced from "deployed from operator's local
   state, didn't notice a side-effect."
3. **No audit trail.** Who deployed, when, what code SHA, what
   resulting CloudFormation state — only present in PROD-DEPLOY-LOG
   if the operator remembered to record it. Several Step entries
   read "deployed today" with no exact timestamp.
4. **No automated verification.** Every deploy was followed by a
   manual `aws lambda invoke` + Slack check. Easy to skip when in a
   hurry; we've already had at least one "deployed but didn't smoke-
   test, then a scheduled run revealed the issue 12 hours later."
5. **Rollback story is by-hand.** `npx sls rollback --timestamp …`
   requires the operator to know the previous deploy timestamp, have
   AWS creds active, have node + sls installed and working. No
   single-button rollback exists.

The promote-to-main release flow (PR #38) made this gap visible: the
release PR has a "post-merge checklist" asking the operator to run
five manual commands. Three of those (sls deploy, mike docs deploy,
SSM config push) are all things a workflow should do automatically.

## Decision

Add a GitHub Actions deployment workflow (`.github/workflows/deploy-prod.yml`)
with the following shape:

- **Trigger:** Tag push matching `v*.*.*` on the `main` branch
  (manual `workflow_dispatch` also allowed for ad-hoc redeploys of
  a chosen tag)
- **Auth:** OIDC role assumption (no long-lived AWS access keys in
  GitHub secrets)
- **Approval gate:** GitHub environment `production` with a required
  reviewer rule (one operator must click *Review deployments → Approve*
  before the deploy step runs)
- **Job 1 — `deploy`** (after approval):
  - Checkout the tagged commit
  - `uv sync --all-extras`
  - Install Serverless Framework + npm deps (`npm ci`)
  - Configure AWS credentials via OIDC
  - `npx sls deploy --stage prod`
  - `uv run backup config push --stage prod` (after deploy so SSM
    config matches the deployed code's schema expectations)
- **Job 2 — `smoke-test`** (depends on `deploy`):
  - Configure AWS credentials via OIDC (read-only role this time)
  - `aws lambda invoke … task_type:health_report` async fire
  - Poll Lambda log group for `Slack delivery ok` / `SNS publish ok`
    within ~6 min window
  - Fail the job if either delivery marker is missing or a
    `Traceback` appears
- **Slack notification:** Job summary posted to operations channel
  on both success and failure (via the same webhook the daily health
  report uses)

### Why tag-trigger rather than main-merge

Tag-trigger ties production deploys to **explicit semver releases**.
Each `v*.*.*` tag is a deliberate "this changeset is going to prod"
decision; each deploy maps to release notes humans wrote. Main-merge
auto-deploy treats every PR merge as a release, which:

- Couples PR-review cadence to deploy frequency (uncomfortable when
  a doc-only PR ships alongside ten code changes)
- Removes the human gating moment where the operator decides "ship
  now"
- Makes the CHANGELOG fight with reality (which PR was the release?)

Tag-trigger preserves the release-notes-as-canonical pattern that
v0.1.0 (PR #38) just established.

### Why OIDC rather than stored access keys

GitHub OIDC has been AWS-recommended for ~2 years for exactly this
case. The trust policy in AWS IAM specifies which GitHub repository
+ branch + workflow can assume the deploy role; no long-lived
secrets exist outside AWS. If a GitHub repo is compromised, the
attacker can't extract a usable AWS credential (they'd have to
modify the workflow itself, which would show in the PR diff). With
stored access keys, leakage = immediate AWS access.

Cost: 30 min one-off setup of the OIDC provider and the deploy
IAM role in the backup account.

### Why manual approval gate

Even with tag-trigger, a wrong tag (typo, accidentally re-tagging
an old commit, force-pushing a tag) shouldn't auto-deploy. The
environment-protection approval rule means a second pair of eyes
sees the tag SHA + release notes before the deploy fires. Adds
~30 seconds; near-zero ongoing cost.

For ad-hoc `workflow_dispatch` invocations, the same approval gate
applies — preventing one operator from accidentally deploying
something unintended.

### Why include post-deploy smoke test

PROD-DEPLOY-LOG Step 17 records the cost of *not* having one:
restore-test temp buckets accumulated for ~24h because the daily
health-report fire detected the issue rather than the deploy
process. A 6-minute smoke test catches Slack delivery failure, IAM
regressions affecting the head_object loop, and "Pydantic config
schema vs SSM blob mismatch" failures inside the deploy window,
before the next scheduled report fires.

### Why include SSM config push in the workflow

Today: separate `backup config push --stage prod` step run manually
after `sls deploy`. Two failure modes possible:

- Operator deploys code but forgets to push config → Lambda runs new
  code against stale config → either Pydantic-rejects (loud) or
  silently uses outdated thresholds (quiet)
- Operator pushes config first but doesn't deploy → Lambda runs old
  code that can't parse new config → silent failure

Including both in the same workflow eliminates the ordering bug.
The YAML committed at the tagged commit is the source of truth for
both Lambda code and SSM config — they always match.

Issue #34 (SSM tier-downgrade silent failure) needs to be fixed
*before* this workflow lands, otherwise the workflow's config-push
step will mask its own failures.

## Cost impact

| Item | Cost |
|---|---|
| GitHub Actions minutes for a deploy run (~7 min including smoke test) | Free on the org's existing plan — Actions usage well below limit |
| OIDC role setup (one-off) | Free — IAM is free |
| Slack webhook for notifications | Free — reuses existing `backup-slack-webhook` secret |
| Lambda invocations from smoke test | <\$0.01/deploy (one invocation) |
| Net ongoing | **\$0/month** |

## Consequences

### Positive

1. **Bus factor goes from 1 to "anyone with repo write access who
   gets the approval rubber-stamp"** — eliminates the holiday-fear
   problem.
2. **Audit trail is automatic** — GitHub Actions logs every deploy
   with timestamp, actor, tag SHA, and CloudFormation outputs.
   PROD-DEPLOY-LOG entries become summaries pointing at run URLs
   rather than the source of truth.
3. **Smoke test catches deploy regressions in-window** rather than at
   the next scheduled fire.
4. **Atomic deploy+config push** — eliminates the ordering bug
   between `sls deploy` and `backup config push`.
5. **Rollback is one-click** — re-run the workflow with a previous
   tag via `workflow_dispatch` and approve.

### Negative

1. **First-deploy migration is risky.** The first run of the new
   workflow must reach feature parity with the current
   manual-deploy state (same IAM, same env vars, same plugin
   versions). Mitigation: run the workflow against a non-prod stage
   first (e.g. `--stage staging` if we have one, otherwise a
   dedicated dry-run stage) and validate equivalence before pointing
   at prod.
2. **Approval gate adds 30 seconds.** For "fix prod RIGHT NOW"
   scenarios, this is real friction. Counter-arguments: (a) the
   gate is also the second-pair-of-eyes; (b) urgent fixes are rare
   enough that 30s isn't material.
3. **OIDC setup requires AWS access** — a one-off operator task in
   the backup account. Documented in the implementation phase.

## Alternatives considered

1. **Status quo (manual sls deploy)** — listed above as the source
   of the operational risks. Rejected.

2. **Main-merge-triggered continuous deployment** — every PR-merge
   becomes a deploy. Considered and rejected for the reasons in
   *"Why tag-trigger rather than main-merge"*. The cleaner narrative
   of "tag = release = deploy" maps better to our actual operational
   cadence (a few deploys per month, not per day).

3. **Stored AWS access key in GitHub secrets** — works, but security
   risk if leaked. Rejected in favour of OIDC.

4. **AWS CodePipeline / CodeBuild instead of GitHub Actions** —
   keeps deploy infrastructure inside AWS. Rejected because:
   - We already have CI in GitHub Actions; using a second system
     duplicates secret management and observability
   - CodePipeline's GitHub trigger has a separate set of
     authentication concerns
   - Team familiarity is on GitHub Actions

5. **Self-hosted runner** — would solve the "AWS-credential
   leakage" angle by running deploys from inside our network.
   Rejected because OIDC already eliminates that risk and self-
   hosted runners add an ongoing maintenance cost (the runner
   machine needs to be patched, monitored, etc.).

6. **Two workflows: deploy then verify** vs one with sequenced
   jobs — chose the latter (one workflow, two jobs `deploy` →
   `smoke-test`) because GitHub Actions handles the dependency
   cleanly and a failed `smoke-test` keeps the job summary in one
   place. Slack notification can still report both phases.

7. **Deploying docs (mike) from the same workflow** — out of scope.
   Mike has its own concerns (gh-pages branch, versioning) and
   deserves a separate small workflow `docs-deploy.yml`. Listed
   under *Out of scope* below to avoid scope creep.

8. **Skipping config push from the deploy workflow** (leave SSM as
   a separate operator step) — rejected for the ordering-bug
   reason in *"Why include SSM config push in the workflow"*.

## Risks

- **OIDC role permissions over-broad** — `sls deploy` requires
  CloudFormation + Lambda + IAM + EventBridge + SNS + S3 + Logs
  permissions, which is a lot. Mitigation: use a managed
  `AWSServerlessFrameworkBoundary`-style policy, or explicitly
  scope to resources with names matching `nzshm-backup-*`. Document
  in the implementation phase.
- **Tag-trigger fires on any `v*.*.*` tag**, including patch tags
  done by mistake on non-main commits. Mitigation: the workflow can
  also check `if: github.ref_name == 'main'` or restrict by tagged
  commit's branch.
- **Smoke-test polling can produce false negatives** if Slack/SES
  delivery happens to take >6 min. Mitigation: empirical baseline
  is ~4-5 min (PROD-DEPLOY-LOG Step 21); the 6-min window has
  headroom. If false negatives become a pattern, extend window.
- **Approval gate person can't approve their own deploy** by GitHub
  default (the actor who pushed the tag can't approve in the same
  environment). For solo-operator periods, this matters. Mitigation:
  configure the approval rule to allow self-approval after a delay,
  OR document an emergency-bypass procedure (operator can manually
  invoke `sls deploy` if absolutely needed).

## Implementation scope

| Component | File | Effort |
|---|---|---|
| OIDC provider + IAM role in backup account | One-off operator task | ~30 min |
| GitHub environment `production` with approval rule | Repo settings UI | ~5 min |
| `.github/workflows/deploy-prod.yml` workflow file | New file | ~90 lines |
| Docs: update `docs/development/release.md` to reference the workflow | Existing file | ~10 lines |
| Docs: new `docs/development/deploy-workflow.md` operator handbook | New file | ~80 lines |
| Update PR #38's post-merge checklist (next release) to use workflow | Future PR | trivial |
| Verify against staging stage before pointing at prod | Manual | ~30 min |
| **Total** | | ~3 h for implementation, plus operator's OIDC setup time |

### Phased rollout

1. **Phase 1**: workflow file + OIDC setup, deploys to a `staging`
   stage (or `--noDeploy` dry-run mode if no staging stack exists).
   Validate parity with manual deploy.
2. **Phase 2**: flip the workflow to `--stage prod` on tag push.
   Manual approval gate active from day one.
3. **Phase 3**: retire the manual deploy instructions from
   `docs/development/release.md` after 3 successful workflow-driven
   deploys.

## Out of scope

- **Mike docs deploy** — separate workflow `docs-deploy.yml` (small
  follow-up after this lands).
- **Pre-deploy schema migrations** — not currently relevant
  (SSM-config is the only stateful schema and Pydantic handles
  forward-compatibility via `extra="ignore"`).
- **Multi-region deploy** — only `ap-southeast-2` today, no
  multi-region requirement.
- **Per-source canary deploys** — out of scope; the daily health
  report's canary already validates each deployment against weka.

## Links

- [docs/development/release.md](../../development/release.md) —
  current manual release process; will be updated after this lands
- [docs/development/lambda-deployment.md](../../development/lambda-deployment.md) —
  current manual Lambda deployment guide
- [PROD-DEPLOY-LOG](../../PROD-DEPLOY-LOG.md) — historical record of
  manual deploys; specifically Steps 14, 17, 18, 19, 21 referenced
  in *Context* above
- Issue #34 — SSM tier-downgrade silent failure (must be fixed
  before this workflow's SSM-push step lands, otherwise the
  workflow masks its own config-push failures)
- AWS docs: [Configuring OpenID Connect in Amazon Web Services](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
