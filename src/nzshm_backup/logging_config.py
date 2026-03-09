"""CloudWatch-compatible logging configuration."""

import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """JSON formatter for CloudWatch Logs integration.

    Outputs log records as JSON for easy parsing in CloudWatch Insights.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        extra = getattr(record, "extra_fields", None)
        if extra:
            log_entry.update(extra)

        return json.dumps(log_entry)


class TextFormatter(logging.Formatter):
    """Human-readable text formatter for CLI usage."""

    def __init__(self, verbose: bool = False):
        super().__init__(
            fmt="%(levelname)s: %(message)s" if verbose else "%(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.verbose = verbose


def setup_logging(
    level: int = logging.INFO,
    json_format: bool = False,
    verbose: bool = False,
) -> logging.Logger:
    """Configure logging for CLI or Lambda runtime.

    Args:
        level: Logging level (default: INFO)
        json_format: Use JSON format for CloudWatch (default: False for CLI)
        verbose: Enable debug output (CLI only)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("nzshm_backup")
    logger.setLevel(level)

    if verbose:
        logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(TextFormatter(verbose=verbose))

    logger.addHandler(handler)

    return logger


def get_logger(name: str = "nzshm_backup") -> logging.Logger:
    """Get logger instance.

    Args:
        name: Logger name (default: nzshm_backup)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)
