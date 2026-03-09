"""Lambda entry point for backup operations."""

import json
from typing import Any

import boto3

from nzshm_backup.config.loader import load_config, load_config_from_env
from nzshm_backup.config.models import ConfigModel
from nzshm_backup.lambda_schema import BackupTask
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.s3_backup import backup_source

logger = setup_logging(json_format=True)


def get_config() -> ConfigModel:
    """Load configuration from environment or file.

    In Lambda runtime, config comes from environment variable.
    For local testing, falls back to YAML file.
    """
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

    logger.info(f"Starting backup task: source={task.source}, dry_run={task.dry_run}")

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

            source_config = config.sources[source_alias]
            region = config.general.region

            account_id = session.client("sts").get_caller_identity()["Account"]

            for bucket_arn in source_config.s3_buckets:
                bucket_name = bucket_arn.split(":")[-1] if ":" in bucket_arn else bucket_arn
                backup_bucket_name = source_config.get_backup_bucket_name(
                    bucket_arn, region, account_id
                )

                logger.info(f"Backing up {bucket_name} → {backup_bucket_name}")

                try:
                    result = backup_source(
                        session=session,
                        source_bucket=bucket_arn,
                        backup_bucket_name=backup_bucket_name,
                        dry_run=task.dry_run,
                        full_sync=task.full_sync,
                    )

                    results[bucket_name] = {
                        "status": "success",
                        "objects_copied": result.objects_copied,
                        "bytes_transferred": result.bytes_transferred,
                        "objects_skipped": result.objects_skipped,
                        "duration_seconds": result.duration_seconds,
                        "dry_run": result.dry_run,
                    }

                except Exception as e:
                    logger.error(f"Backup failed for {bucket_name}: {e}")
                    results[bucket_name] = {
                        "status": "error",
                        "error": str(e),
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
