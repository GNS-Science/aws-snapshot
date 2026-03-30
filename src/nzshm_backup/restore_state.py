"""SSM-based state for pending DynamoDB PITR restores awaiting PITR re-enable."""

import json
import logging
from datetime import datetime, timezone

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

SSM_PARAM_NAME = "/nzshm-backup/pending-restores"


def read_pending_restores(ssm_client) -> list[dict]:
    """Read pending restore entries from SSM. Returns empty list if parameter absent."""
    try:
        resp = ssm_client.get_parameter(Name=SSM_PARAM_NAME)
        data = json.loads(resp["Parameter"]["Value"])
        return list(data.get("pending", []))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return []
        raise


def write_pending_restores(ssm_client, entries: list[dict]) -> None:
    """Overwrite the pending restore list in SSM."""
    ssm_client.put_parameter(
        Name=SSM_PARAM_NAME,
        Value=json.dumps({"pending": entries}),
        Type="String",
        Overwrite=True,
    )


def add_pending_restore(
    ssm_client,
    restore_arn: str,
    source: str,
    source_table_arn: str = "",
    restore_point_iso: str = "",
) -> None:
    """Append one entry to the pending restore list."""
    entries = read_pending_restores(ssm_client)
    entries.append(
        {
            "restore_arn": restore_arn,
            "source": source,
            "source_table_arn": source_table_arn,
            "restore_point": restore_point_iso,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_pending_restores(ssm_client, entries)
    logger.info(f"Recorded pending restore in SSM: {restore_arn}")
