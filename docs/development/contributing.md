# Contributing

## Setup

```bash
git clone https://github.com/gns-science/nzshm-backup.git
cd nzshm-backup
uv sync --all-extras      # installs all deps including dev and docs extras
```

The project uses a `src/` layout. The installed package is `aws-snapshot`; the
Python import path is `aws_snapshot`.

> **Note:** The project uses `uv` for dependency management. `uv.lock` is the
> source of truth for pinned versions.

## Running the CLI locally

```bash
uv run backup --help
uv run backup status --source toshi --dry-run
```

Or activate the virtual environment once:

```bash
source .venv/bin/activate
backup --help
```

## Common workflows

```bash
make test      # run pytest
make lint      # ruff check + mypy
make fmt       # ruff format + ruff check --fix
make check     # lint then test — run before committing
make upgrade   # upgrade deps (1-week cooldown — see pyproject.toml [tool.uv])
make sync      # re-sync venv after pulling (uv sync --all-extras)
```

## Code style

| Tool | Purpose | Config |
|------|---------|--------|
| `ruff` | Formatter + linter (line length 100) | `[tool.ruff]` in `pyproject.toml` |
| `mypy` | Static type checker | `[tool.mypy]` in `pyproject.toml` |

## Tests

```bash
make test
uv run pytest tests/test_s3_backup.py -v
uv run pytest -k "test_dry_run"
```

See [Development: Testing](testing.md) for the full test guide.

## Upgrading dependencies

Always use the 1-week safety margin to avoid picking up packages released in the last 7 days:

```bash
make upgrade
```

This runs `uv lock --upgrade` then re-syncs the environment. The 1-week dependency cooldown is configured in `[tool.uv]` in `pyproject.toml` — see [uv dependency cooldowns](https://docs.astral.sh/uv/concepts/resolution/#dependency-cooldowns). Document results in `docs/development/UPDATE_REPORT_<date>.md`.

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

1. Create `src/aws_snapshot/commands/my_command.py` with a `typer.Typer()` app
2. Add the command in `cli.py`:
   ```python
   from aws_snapshot.commands.my_command import app as my_app
   app.add_typer(my_app, name="my-cmd", help="Description.")
   ```
3. Add tests in `tests/test_my_command.py`
4. Document in `docs/user-guide/` or `docs/cli-reference.md`

## Architecture guide

See [Architecture Overview](../architecture/overview.md) and the design docs for
the rationale behind key decisions (CLI-first, Typer, account isolation, etc.).
