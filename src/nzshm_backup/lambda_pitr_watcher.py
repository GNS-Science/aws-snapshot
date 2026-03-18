"""Lambda entry point: poll SSM for pending DynamoDB PITR restores and re-enable PITR."""

import logging
from collections import defaultdict
from typing import Any

import boto3

from nzshm_backup.config.loader import load_config, load_config_from_env, load_config_from_ssm
from nzshm_backup.config.models import ConfigModel
from nzshm_backup.dynamodb_restore import PITR_WATCHER_RULE_NAME
from nzshm_backup.logging_config import setup_logging
from nzshm_backup.restore_state import read_pending_restores, write_pending_restores
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


def _process_source_entries(
    dynamodb_client,
    entries: list[dict],
    source_alias: str,
) -> tuple[list[dict], int]:
    """Enable PITR on any ACTIVE tables; return (remaining_entries, still_pending).

    Args:
        dynamodb_client: boto3 DynamoDB client scoped to the source account.
        entries:         Pending restore entries for this source from SSM.
        source_alias:    Human-readable source name for log messages.

    Returns:
        (remaining, still_pending): entries not yet completed and count thereof.
    """
    remaining = []
    still_pending = 0

    for entry in entries:
        restore_arn = entry["restore_arn"]
        table_name = restore_arn.split("/")[-1]

        try:
            resp = dynamodb_client.describe_table(TableName=table_name)
            status = resp["Table"]["TableStatus"]

            if status == "ACTIVE":
                dynamodb_client.update_continuous_backups(
                    TableName=table_name,
                    PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
                )
                tags = [
                    {"Key": "RestoredBy",   "Value": "nzshm-backup"},
                    {"Key": "RestoredFrom", "Value": entry.get("source_table_arn", "").split("/")[-1]},
                    {"Key": "RestoredAt",   "Value": entry.get("restore_point", "")},
                ]
                try:
                    dynamodb_client.tag_resource(ResourceArn=restore_arn, Tags=tags)
                except Exception as tag_err:
                    logger.warning(f"[{source_alias}] Could not tag {table_name}: {tag_err}")
                logger.info(f"[{source_alias}] PITR enabled and tagged: {table_name}")
                # Entry is done — do not add to remaining
            else:
                still_pending += 1
                remaining.append(entry)
                logger.info(f"[{source_alias}] {table_name} still {status} — will retry")

        except Exception as e:
            logger.error(f"[{source_alias}] Failed to process {table_name}: {e}")
            still_pending += 1
            remaining.append(entry)

    return remaining, still_pending


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Poll SSM for pending DynamoDB PITR restores and re-enable PITR on ACTIVE tables.

    Reads /nzshm-backup/pending-restores from SSM Parameter Store, processes each
    entry, removes completed ones, and disables the EventBridge rule once all
    pending restores have completed.
    """
    config = _get_config()
    session = boto3.Session()
    backup_account_id = get_account_id(session)
    ssm_client = session.client("ssm")

    pending = read_pending_restores(ssm_client)
    if not pending:
        logger.info("No pending restores in SSM — disabling pitr-watcher rule")
        session.client("events").disable_rule(Name=PITR_WATCHER_RULE_NAME)
        return {"statusCode": 200, "tables_found": 0, "still_pending": 0}

    # Group entries by source alias
    by_source: dict[str, list[dict]] = defaultdict(list)
    for entry in pending:
        by_source[entry["source"]].append(entry)

    remaining_all: list[dict] = []
    total_still_pending = 0

    for source_alias, entries in by_source.items():
        source_config = config.sources.get(source_alias)
        if source_config is None:
            logger.warning(f"Unknown source '{source_alias}' in SSM pending list — keeping entries")
            remaining_all.extend(entries)
            total_still_pending += len(entries)
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

        remaining, still_pending = _process_source_entries(
            dynamodb_client=source_session.client("dynamodb"),
            entries=entries,
            source_alias=source_alias,
        )
        remaining_all.extend(remaining)
        total_still_pending += still_pending

    write_pending_restores(ssm_client, remaining_all)

    if total_still_pending == 0:
        logger.info("No pending restores remaining — disabling pitr-watcher rule")
        session.client("events").disable_rule(Name=PITR_WATCHER_RULE_NAME)

    return {
        "statusCode": 200,
        "tables_found": len(pending),
        "still_pending": total_still_pending,
    }
