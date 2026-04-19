"""Lambda entry point for backup operations."""

import json
from typing import Any

import boto3

from nzshm_backup.backup_engine import run_backup_source
from nzshm_backup.config.loader import load_config, load_config_from_env, load_config_from_ssm
from nzshm_backup.config.models import ConfigModel
from nzshm_backup.lambda_schema import BackupTask
from nzshm_backup.logging_config import setup_logging

logger = setup_logging(json_format=True)


def get_config() -> ConfigModel:
    """Load configuration from SSM, environment variable, or file.

    Resolution order:
    1. SSM Parameter Store (if NZSHM_STAGE env var is set)
    2. BACKUP_CONFIG environment variable (JSON)
    3. Local YAML file (for local CLI usage)
    """
    import os

    stage = os.environ.get("NZSHM_STAGE")
    if stage:
        try:
            return load_config_from_ssm(stage)
        except FileNotFoundError:
            logger.info(
                f"SSM parameter not found for stage '{stage}', falling back to env/file config"
            )
    try:
        return load_config_from_env()
    except ValueError:
        logger.info("Environment config not found, loading from file")
        return load_config()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for backup operations.

    Args:
        event: EventBridge event containing BackupTask parameters
        context: Lambda context object

    Returns:
        Dict with statusCode and body for API Gateway compatibility
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        task = BackupTask.model_validate(event)
    except Exception as e:
        logger.error(f"Invalid event format: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Invalid event format: {str(e)}"}),
        }

    logger.info(
        "Starting backup task: "
        f"source={task.source}, dry_run={task.dry_run}, prepare_only={task.prepare_only}"
    )

    try:
        config = get_config()
        session = boto3.Session()

        results = {}

        if task.source == "all":
            sources_to_backup = list(config.sources.keys())
        else:
            sources_to_backup = [task.source]

        for source_alias in sources_to_backup:
            if source_alias not in config.sources:
                logger.error(f"Unknown source alias: {source_alias}")
                results[source_alias] = {"error": f"Unknown source: {source_alias}"}
                continue

            source_result = run_backup_source(
                session,
                config,
                source_alias,
                dry_run=task.dry_run,
                full_sync=task.full_sync,
                prepare_only=task.prepare_only,
            )

            for r in source_result.s3_results:
                results[r.get("bucket_name", source_alias)] = {
                    k: v for k, v in r.items() if k != "bucket_name"
                }

            for r in source_result.dynamodb_results:
                results[r.get("table_name", source_alias)] = {
                    k: v for k, v in r.items() if k != "table_name"
                }

        success = all(r.get("status") == "success" or "error" not in r for r in results.values())

        return {
            "statusCode": 200 if success else 500,
            "body": json.dumps(
                {
                    "task": task.model_dump(),
                    "results": results,
                    "success": success,
                }
            ),
        }

    except Exception as e:
        logger.exception(f"Backup failed: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
