"""CLI command groups for NSHM Backup."""

from nzshm_backup.commands.schedule import schedule
from nzshm_backup.commands.run_backup import run
from nzshm_backup.commands.restore import restore
from nzshm_backup.commands.test import test
from nzshm_backup.commands.status import status
from nzshm_backup.commands.report import report
from nzshm_backup.commands.costs import costs
from nzshm_backup.commands.config import config

__all__ = [
    "schedule",
    "run",
    "restore",
    "test",
    "status",
    "report",
    "costs",
    "config",
]
