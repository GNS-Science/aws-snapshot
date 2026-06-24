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

### SAM (preferred, post-#48 migration)

```bash
eval "$(aws configure export-credentials --profile backup-account --format env)"
cp samconfig.example.toml samconfig.toml   # first time only — then edit parameter_overrides
sam build --use-container
sam deploy
```

See [Lambda Deployment](lambda-deployment.md) for prerequisites (Docker, `aws-sam-cli`).

For the **one-time cutover** from Serverless Framework to SAM (Activity B),
see [SAM cutover runbook](sam-cutover-runbook.md). For first-time SAM
verification — `sam validate`, `sam build`, side-stack deploy, and parity
diff vs the legacy sls stack — see
[SAM deploy verification](sam-deploy-verification.md). The verification
runbook is a prerequisite for the cutover runbook.

### Serverless Framework (legacy — to be removed once SAM parity is confirmed)

```bash
eval "$(aws configure export-credentials --profile backup-account --format env)"
sls deploy --stage prod
```

Both deploy paths exist in the repo during the SAM transition (issue #48). Once
the SAM template has been used for at least one real production deploy and
verified equivalent, `serverless.yml` is removed in a follow-up PR.
