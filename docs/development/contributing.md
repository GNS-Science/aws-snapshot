# Contributing

## Setup

```bash
git clone https://github.com/gns-science/nzshm-backup.git
cd nzshm-backup
poetry install         # installs all deps including dev extras
```

The project uses a `src/` layout. The installed package is `nzshm-backup`; the
Python import path is `nzshm_backup`.

## Running the CLI locally

```bash
poetry run backup --help
poetry run backup status --source toshi --dry-run
```

Or activate the virtual environment once:

```bash
poetry shell
backup --help
```

## Code style

| Tool | Purpose | Config |
|------|---------|--------|
| `black` | Formatter (line length 100) | `pyproject.toml` |
| `ruff` | Linter — E, F, W, I, N, UP, B, C4 rules | `pyproject.toml` |
| `mypy` | Static type checker | `pyproject.toml` |

Run all checks:

```bash
poetry run ruff check src/ tests/
poetry run black src/ tests/
poetry run mypy src/
```

Fix auto-fixable lint issues:

```bash
poetry run ruff check --fix src/ tests/
poetry run black src/ tests/
```

## Tests

```bash
poetry run pytest
poetry run pytest tests/test_s3_backup.py -v
poetry run pytest -k "test_dry_run"
```

See [Development: Testing](testing.md) for the full test guide.

## Commit style

Propose a commit after each logical unit of work is verified. Use conventional
commit prefixes:

```
feat: add S3 Batch restore job status to 'restore status' command
fix: canonical restore bucket name (63-char limit)
refactor: unify restore suffix to -restore for both S3 and DynamoDB
docs: DR scenario — parallel forensics rationale
test: add integrity check for DynamoDB export verification
```

Commit granularity: one logical change per commit. Do not batch unrelated changes.

## Adding a new subcommand

1. Create `src/nzshm_backup/commands/my_command.py` with a `typer.Typer()` app
2. Add the command in `cli.py`:
   ```python
   from nzshm_backup.commands.my_command import app as my_app
   app.add_typer(my_app, name="my-cmd", help="Description.")
   ```
3. Add tests in `tests/test_my_command.py`
4. Document in `docs/user-guide/` or `docs/cli-reference.md`

## Architecture guide

See [Architecture Overview](../architecture/overview.md) and the design docs for
the rationale behind key decisions (CLI-first, Typer, account isolation, etc.).
