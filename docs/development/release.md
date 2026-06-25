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

## Release steps

1. **Run tests and checks**:
   ```bash
   uv run tox --skip-missing-interpreters -e py312,format,lint,build-linux
   ```

2. **Tag and push**:
   ```bash
   git tag v0.4.0
   git push origin main --tags
   ```

   The version is derived from the tag — no file edits needed.

3. **Build and publish** (if publishing to PyPI):
   ```bash
   uv build
   uv publish
   ```

## Docs versioning with mike

The docs site uses [mike](https://github.com/jimporter/mike) for versioned docs
(configured in `mkdocs.yml` under `extra.version`).

```bash
# Deploy a new version
uv run mike deploy 0.4.0 latest --update-aliases --push

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
