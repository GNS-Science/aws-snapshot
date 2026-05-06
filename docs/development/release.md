# Release Process

## Versioning

The project follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).
Version is set in `pyproject.toml` and exposed via `nzshm_backup/__init__.py`:

```python
__version__ = "0.3.0"
```

Display with:

```bash
backup --version
```

## Release steps

1. **Update version** in `pyproject.toml`:
   ```toml
   [project]
   version = "0.4.0"
   ```

2. **Update `__init__.py`**:
   ```python
   __version__ = "0.4.0"
   ```

3. **Run tests and checks**:
   ```bash
   uv run pytest
   uv run ruff check src/ tests/
   uv run mypy src/
   ```

4. **Commit and tag**:
   ```bash
   git add pyproject.toml src/nzshm_backup/__init__.py
   git commit -m "chore: bump version to 0.4.0"
   git tag v0.4.0
   git push origin main --tags
   ```

5. **Build and publish** (if publishing to PyPI):
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

After bumping the version, redeploy the Lambda so it reports the new version:

```bash
eval "$(aws configure export-credentials --profile backup-account --format env)"
export BACKUP_CONFIG=$(...yaml to json...)
sls deploy --stage prod
```

See [Lambda Deployment](lambda-deployment.md) for full instructions.
