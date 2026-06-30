# Release Process

## Versioning

The project follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).
Version is derived automatically from git tags via `hatch-vcs`. No manual version
strings in source code — the version is generated at build time from the most
recent `vX.Y.Z` tag.

Display with:

```bash
uv run backup --version
```

Between tags, dev builds get versions like `0.1.1.dev3+gabcdef1`.

## One-time setup (operator, on PyPI)

The first release of `aws-snapshot` to PyPI required a one-time
Trusted Publisher configuration. If you're cutting a release for the
first time on a new account or after a project transfer, repeat
these steps:

1. **Ensure the project exists on PyPI.** Either a previous release,
   a manual placeholder upload, or an initial release flow that
   creates it.
2. **Configure a Trusted Publisher on the PyPI project page**:
   - Sign in to https://pypi.org.
   - Navigate to *Project* → *Manage* → *Publishing*.
   - Add a new *pending publisher* (or regular publisher if the
     project already exists) with:

     | Field | Value |
     |---|---|
     | Owner | `GNS-Science` |
     | Repository name | `aws-snapshot` |
     | Workflow name | `release.yml` |
     | Environment | `pypi` |

3. **Configure the `pypi` GitHub Environment in this repo**:
   - *Settings* → *Environments* → *New environment* → `pypi`.
   - Add at least one **required reviewer** (the release workflow's
     publish job pauses for manual approval before posting to PyPI;
     anyone on the reviewer list can approve).

No API tokens or secrets are stored anywhere — Trusted Publishing
uses short-lived OIDC tokens issued by GitHub Actions and exchanged
for upload credentials at publish time.

## Releasing

Once one-time setup is in place, a release is a tag push:

1. **Run tests and checks** locally first if you want:

    ```bash
    uv run tox --skip-missing-interpreters -e py312,format,lint,build-linux
    ```

2. **Update the CHANGELOG** — move the `## Unreleased` block under a
   new `## [vX.Y.Z] - YYYY-MM-DD` header. Commit on `main` via PR
   (this is the only commit that touches `CHANGELOG.md`).

3. **Tag and push**:

    ```bash
    git tag vX.Y.Z
    git push origin vX.Y.Z
    ```

   The version is derived from the tag — no file edits needed in
   source.

4. **Approve the publish step** in GitHub Actions:
   - The `release.yml` workflow's `publish` job pauses on the `pypi`
     environment for reviewer approval.
   - Click *Review deployments* on the workflow run and approve.

5. **Verify**:
   - PyPI page updates within a minute:
     https://pypi.org/project/aws-snapshot/
   - GitHub Release auto-created at
     https://github.com/GNS-Science/aws-snapshot/releases with
     auto-generated notes and the sdist + wheel attached.

## Manual fallback

If Trusted Publishing is unavailable for some reason (PyPI outage,
OIDC misconfig, etc.), the workflow can be bypassed and built /
published locally:

```bash
uv build
uv publish  # requires a PyPI API token in ~/.pypirc or env
```

This is the previous-generation flow; prefer the tag-triggered
workflow unless something is broken.

## Docs versioning with mike

The docs site uses [mike](https://github.com/jimporter/mike) for versioned docs
(configured in `mkdocs.yml` under `extra.version`).

```bash
# Deploy a new version
uv run mike deploy X.Y.Z latest --update-aliases --push

# List deployed versions
uv run mike list

# Set the default version shown on the landing page
uv run mike set-default latest --push
```

Docs are served from the `gh-pages` branch.

## Lambda deployment after release

After bumping the version, redeploy the Lambda so it reports the new version.

```bash
eval "$(aws configure export-credentials --profile <aws-profile> --format env)"
cp samconfig.example.toml samconfig.toml   # first time only — then edit parameter_overrides
make sam-build
sam deploy
```

See [Lambda Deployment](lambda-deployment.md) for prerequisites (Docker,
SAM CLI is bundled via the dev dependency group). The
[SAM deploy verification](sam-deploy-verification.md) runbook walks through
a side-stack `sam build` / `sam deploy` against a real AWS account if
you want a parity check before deploying to prod.

The one-time historical cutover from Serverless Framework to SAM is
recorded in the [SAM cutover runbook](sam-cutover-runbook.md) and was
executed 2026-06-24; the runbook is preserved as a reference in case a
future restore-from-cold-start scenario requires re-deploying the stack
from scratch.
