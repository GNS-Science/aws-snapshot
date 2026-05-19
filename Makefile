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
	uv run mypy src/

fmt:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

check: lint test
