from __future__ import annotations

import logging


class HealthCheckAccessLogFilter(logging.Filter):
    """Silences the ASGI access log line for the health check endpoint.

    Args:
        record: The log record to evaluate.

    Returns:
        False to drop the record, True to let it through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO:
            return True
        if "/health/" not in record.getMessage():
            return True
        return logging.getLogger().isEnabledFor(logging.DEBUG)
