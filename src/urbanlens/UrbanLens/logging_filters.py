from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.security.redact import redact_email_addresses, redact_urls

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Set on a request whose 503 is an answer the view chose ("ask again after Retry-After"), not a failure.
RETRY_LATER_ATTRIBUTE = "ul_retry_later"

_TRACEBACKS = logging.Formatter()


def _redact(text: str) -> str:
    return redact_email_addresses(redact_urls(text))


def mark_retry_later(request: HttpRequest) -> None:
    """Keep this request's 503 out of ``django.request``'s errors; for a view whose 503 means "not yet, ask again".

    Args:
        request: The request being answered with 503 and ``Retry-After``.
    """
    setattr(request, RETRY_LATER_ATTRIBUTE, True)


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
    """Replace the credentials and coordinates in any URL a record would print, and any email address, in its message and its traceback.

    Belongs on every handler: a ``requests`` error's text is its URL, query-string API key included, so any logger
    handed one carries it, and an SMTP refusal's text names the addresses it refused.
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
        if message is not None and (redacted := _redact(message)) != message:
            record.msg, record.args = redacted, ()
        try:
            if isinstance(record.exc_info, tuple) and record.exc_info[0] is not None and not record.exc_text:
                record.exc_text = _TRACEBACKS.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _redact(record.exc_text)
        except Exception:
            record.exc_info, record.exc_text = None, "<traceback withheld: it could not be redacted>"
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        return True


def redact_every_handler() -> None:
    """Give every handler any logger holds a :class:`SecretRedactionFilter`, for handlers configured outside ``LOGGING``."""
    loggers = [logging.getLogger(), *(logger for logger in list(logging.Logger.manager.loggerDict.values()) if isinstance(logger, logging.Logger))]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(existing, SecretRedactionFilter) for existing in handler.filters):
                handler.addFilter(SecretRedactionFilter())


class RetryLaterFilter(logging.Filter):
    """Drop ``django.request``'s record of a 503 its view marked with :data:`RETRY_LATER_ATTRIBUTE`."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep every record but a marked request's 503.

        Args:
            record: A ``django.request`` record, which carries ``status_code`` and ``request``.

        Returns:
            False for the deliberate 503, True otherwise.
        """
        if getattr(record, "status_code", None) != 503:
            return True
        return not getattr(getattr(record, "request", None), RETRY_LATER_ATTRIBUTE, False)
