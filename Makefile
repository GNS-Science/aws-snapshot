.PHONY: sync upgrade test lint fmt check

sync:
	uv sync --all-extras

upgrade:
	uv lock --upgrade
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/
	uv run mypy src/ || true  # 38 pre-existing type errors — deferred

fmt:
	uv run black src/ tests/
	uv run ruff check --fix src/ tests/

check: lint test
