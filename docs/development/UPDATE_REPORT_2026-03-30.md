# Dependency Update Report — 30 March 2026

## Method

Used `uv` with a 1-week recency filter to avoid upgrading packages released within the last 7 days:

```bash
uv lock --upgrade   # cooldown: exclude-newer = "1 week" set in pyproject.toml
uv sync --all-extras
uv run pytest
```

This also migrated the lock file from `poetry.lock` to `uv.lock`.

## Upgraded

| Package | From | To | Released | Notes |
|---------|------|----|----------|-------|
| `ruff` | unpinned | 0.15.7 | 2026-03-19 | Also fixed `[tool.ruff.lint]` deprecation in pyproject.toml |
| `mkdocs-get-deps` | unpinned | 0.2.2 | 2026-03-10 | Transitive (mkdocs) |
| `cryptography` | unpinned | 46.0.5 | — | Transitive |
| `griffelib` | unpinned | 2.0.0 | — | Transitive |
| `pymdown-extensions` | unpinned | 10.21 | — | Transitive (mkdocs-material) |
| `requests` | unpinned | 2.32.5 | — | Transitive |
| `werkzeug` | unpinned | 3.1.6 | — | Transitive |
| `tomli` | unpinned | 2.4.0 | — | Transitive |
| `boto3` / `botocore` | 1.42.78* | 1.42.73 | 2026-03-17 | *uv had auto-resolved to 1.42.78; pinned back to pre-cutoff |
| `pygments` | 2.20.0* | 2.19.2 | — | *same as above |

## Skipped (post-cutoff — check next week)

| Package | Latest | Released | Reason |
|---------|--------|----------|--------|
| `boto3` / `botocore` | 1.42.78 | 2026-03-27 | Released after cutoff |
| `pydantic-core` | 2.44.0 | 2026-03-27 | Released after cutoff |
| `pygments` | 2.20.0 | 2026-03-29 | Released after cutoff |
| `ruff` | 0.15.8 | 2026-03-26 | Released after cutoff |

## Verification

200 tests passing, no regressions.

## Notes

- `poetry.lock` is superseded by `uv.lock`. Run `uv sync --all-extras` instead of `poetry install`.
- The `--exclude-newer` flag handles date filtering automatically; no need to check PyPI release dates manually. The cutoff is now set centrally via `[tool.uv] exclude-newer = "1 week"` in `pyproject.toml` (see [uv dependency cooldowns](https://docs.astral.sh/uv/concepts/resolution/#dependency-cooldowns)), so `make upgrade` no longer needs to pass the flag explicitly.
- The previously unpinned dev/transitive packages (`coverage`, `black`, `pytest-cov`, `mkdocs-material`, `charset-normalizer`) are now pinned in `uv.lock` at their pre-cutoff versions.
