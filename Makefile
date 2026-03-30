EXCLUDE_NEWER := $(shell date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)

.PHONY: sync upgrade test lint fmt check

sync:
	uv sync --all-extras

upgrade:
	uv lock --upgrade --exclude-newer $(EXCLUDE_NEWER)
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/
	uv run mypy src/

fmt:
	uv run black src/ tests/
	uv run ruff check --fix src/ tests/

check: lint test
