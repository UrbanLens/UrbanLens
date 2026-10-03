from __future__ import annotations

import logging

from urbanlens.dashboard.services.security.redact import redact_urls

_TRACEBACKS = logging.Formatter()


class HealthCheckAccessLogFilter(logging.Filter):
    """Drop ASGI access log lines for the health endpoint.

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


class SecretRedactionFilter(logging.Filter):
    """Replace the credentials and coordinates in any URL a record would print, in its message and its traceback.

    Belongs on every handler: a ``requests`` error's text is its URL, query-string API key included, so any logger
    handed one carries it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact ``record`` in place, so every later handler writes the redacted text.

        Args:
            record: The log record about to be written.

        Returns:
            Always True.
        """
        try:
            message = record.getMessage()
        except Exception:
            # The handler reports a record it cannot format.
            message = None
        if message is not None and (redacted := redact_urls(message)) != message:
            record.msg, record.args = redacted, ()
        try:
            if isinstance(record.exc_info, tuple) and record.exc_info[0] is not None and not record.exc_text:
                record.exc_text = _TRACEBACKS.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_urls(record.exc_text)
        except Exception:
            record.exc_info, record.exc_text = None, "<traceback withheld: it could not be redacted>"
        if record.stack_info:
            record.stack_info = redact_urls(record.stack_info)
        return True


def redact_every_handler() -> None:
    """Give every handler any logger holds a :class:`SecretRedactionFilter`, for handlers configured outside ``LOGGING``."""
    loggers = [logging.getLogger(), *(logger for logger in list(logging.Logger.manager.loggerDict.values()) if isinstance(logger, logging.Logger))]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(existing, SecretRedactionFilter) for existing in handler.filters):
                handler.addFilter(SecretRedactionFilter())
