"""Lambda entry point: poll for PITRPending=true DynamoDB tables and re-enable PITR."""

import logging
from typing import Any

import boto3

from nzshm_backup.config.loader import load_config, load_config_from_env, load_config_from_ssm
from nzshm_backup.config.models import ConfigModel
from nzshm_backup.dynamodb_restore import PITR_PENDING_TAG, PITR_WATCHER_RULE_NAME
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.s3_backup import get_account_id, get_cross_account_session

logger = setup_logging(json_format=True)


def _get_config() -> ConfigModel:
    import os

    stage = os.environ.get("NZSHM_STAGE")
    if stage:
        try:
            return load_config_from_ssm(stage)
        except FileNotFoundError:
            logger.info(f"SSM config not found for stage '{stage}', falling back")
    try:
        return load_config_from_env()
    except ValueError:
        return load_config()


def _process_source(
    tagging_client,
    dynamodb_client,
    source_alias: str,
) -> tuple[int, int]:
    """Find PITRPending=true tables in one account and enable PITR on any that are ACTIVE.

    Args:
        tagging_client:  ResourceGroupsTaggingAPI client scoped to the source account.
        dynamodb_client: DynamoDB client scoped to the source account.
        source_alias:    Human-readable source name for log messages.

    Returns:
        (found, still_pending): count of tagged tables found and count not yet ACTIVE.
    """
    pending: list[tuple[str, str]] = []  # (table_name, table_arn)
    paginator = tagging_client.get_paginator("get_resources")
    for page in paginator.paginate(
        TagFilters=[{"Key": PITR_PENDING_TAG, "Values": ["true"]}],
        ResourceTypeFilters=["dynamodb:table"],
    ):
        for resource in page.get("ResourceTagMappingList", []):
            table_arn = resource["ResourceARN"]
            table_name = table_arn.split("/")[-1]
            pending.append((table_name, table_arn))

    still_pending = 0
    for table_name, table_arn in pending:
        try:
            resp = dynamodb_client.describe_table(TableName=table_name)
            status = resp["Table"]["TableStatus"]

            if status == "ACTIVE":
                dynamodb_client.update_continuous_backups(
                    TableName=table_name,
                    PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
                )
                dynamodb_client.untag_resource(
                    ResourceArn=table_arn,
                    TagKeys=[PITR_PENDING_TAG],
                )
                logger.info(f"[{source_alias}] PITR enabled and tag removed: {table_name}")
            else:
                still_pending += 1
                logger.info(f"[{source_alias}] {table_name} still {status} — will retry")
        except Exception as e:
            logger.error(f"[{source_alias}] Failed to process {table_name}: {e}")
            still_pending += 1

    return len(pending), still_pending


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Poll all configured sources for PITRPending=true tables and re-enable PITR.

    Disables the EventBridge rule that triggers this Lambda once all pending
    restores have completed (no PITRPending=true tables remain in any source).
    """
    config = _get_config()
    session = boto3.Session()
    backup_account_id = get_account_id(session)

    total_found = 0
    total_still_pending = 0

    for source_alias, source_config in config.sources.items():
        if not source_config.dynamodb_tables:
            continue

        source_account_id = source_config.source_account_id or backup_account_id
        restore_role_arn = (
            source_config.source_account_restore_role_arn
            or source_config.source_account_role_arn
        )
        source_session = (
            get_cross_account_session(session, restore_role_arn)
            if restore_role_arn and backup_account_id != source_account_id
            else session
        )

        found, still_pending = _process_source(
            tagging_client=source_session.client("resourcegroupstaggingapi"),
            dynamodb_client=source_session.client("dynamodb"),
            source_alias=source_alias,
        )
        total_found += found
        total_still_pending += still_pending

    if total_still_pending == 0:
        logger.info("No pending restores remaining — disabling pitr-watcher rule")
        session.client("events").disable_rule(Name=PITR_WATCHER_RULE_NAME)

    return {
        "statusCode": 200,
        "tables_found": total_found,
        "still_pending": total_still_pending,
    }
