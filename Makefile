.PHONY: sync upgrade test lint fmt check \
        sam-prepare sam-build \
        build-PitrWatcherFunction build-BackupFunction build-AlarmBridgeFunction \
        _sam_build_one

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

# ---------------------------------------------------------------------------
# SAM build targets — one per AWS::Serverless::Function logical ID in
# sam.yaml. Invoked by `sam build` (template uses BuildMethod: makefile).
# ---------------------------------------------------------------------------
# Why Makefile build instead of SAM's default python3.10 BuildMethod:
#   - Default builder copies the entire CodeUri (project root) into the
#     artefact — bloats it with docs/, tests/, .venv/, AND ships
#     backup-config.production.yaml. Unacceptable.
#   - .aws-samignore is documented but not honored by PythonPipBuilder
#     during CopySource (see aws-lambda-builders).
#   - This Makefile produces exactly the artefact each function needs:
#     `nzshm_backup/` package contents at root + pip deps from uv.lock.

# Host-side: generate requirements.txt from uv.lock. Run this before
# `sam build` (or use `make sam-build` which wraps both). The build
# targets below run INSIDE SAM's container which has python3.10 + pip
# but not uv — so requirements.txt must be pre-generated on the host.
sam-prepare:
	uv export --format requirements-txt --no-emit-project --no-dev -o requirements.txt

# Convenience: prepare + sam build in one go.
sam-build: sam-prepare
	sam build --use-container

# Internal helper called by each function's build target. Installs deps
# into ARTIFACTS_DIR and copies nzshm_backup/ at root so the Handler
# module path `nzshm_backup.lambda_handler.handler` resolves without
# a PYTHONPATH workaround. Runs inside SAM's build container.
_sam_build_one:
	@mkdir -p $(ARTIFACTS_DIR)
	@pip install --quiet --target $(ARTIFACTS_DIR) -r requirements.txt
	@cp -r src/nzshm_backup $(ARTIFACTS_DIR)/nzshm_backup

build-PitrWatcherFunction:
	@$(MAKE) _sam_build_one

build-BackupFunction:
	@$(MAKE) _sam_build_one

build-AlarmBridgeFunction:
	@$(MAKE) _sam_build_one
