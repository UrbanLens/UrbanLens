"""Read what the configured log handlers write, as an operator would see it in the log."""

from __future__ import annotations

from contextlib import contextmanager
import io
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def handler_output(*logger_names: str) -> Iterator[io.StringIO]:
    """Everything the configured stream handlers of ``logger_names`` write while the block runs.

    Swaps each handler's stream rather than adding a handler of its own, so a record passes the real filters and
    formatter on its way to the buffer. ``""`` names the root logger.

    Args:
        logger_names: The loggers whose handlers to read.

    Yields:
        The buffer the handlers write to.

    Raises:
        AssertionError: None of the loggers has a stream handler, so nothing could be read.
    """
    buffer = io.StringIO()
    swapped: list[tuple[logging.StreamHandler[Any], Any]] = []
    for name in logger_names:
        for handler in logging.getLogger(name).handlers:
            if isinstance(handler, logging.StreamHandler) and all(handler is not seen for seen, _ in swapped):
                swapped.append((handler, handler.setStream(buffer)))
    if not swapped:
        msg = f"no stream handler is configured on {logger_names}"
        raise AssertionError(msg)
    try:
        yield buffer
    finally:
        for handler, previous in swapped:
            if previous is not None:
                handler.setStream(previous)
