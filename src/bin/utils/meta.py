from __future__ import annotations

from typing import Literal, Required, TypedDict


class Logger(TypedDict, total=False):
    """
    Expected format for a "logger" in the settings file.
    """

    level: int | str
    handlers: list[str]
    propagate: bool


class LogFormatter(TypedDict, total=False):
    format: str


LogHandler = TypedDict(
    "LogHandler",
    {
        "class": str,
        "level": int | str,
        "formatter": str,
        "stream": str,
    },
    total=False,
)


class LogRoot(TypedDict, total=False):
    level: int | str
    handlers: list[str]


class SettingsLog(TypedDict, total=False):
    """
    Expected format for the logging portion of the settings file.

    This mirrors the schema accepted by ``logging.config.dictConfig``, so a valid
    ``SettingsLog`` can be passed straight through to it.
    """

    version: Required[Literal[1]]
    formatters: dict[str, LogFormatter]
    handlers: dict[str, LogHandler]
    loggers: dict[str, Logger]
    root: LogRoot


class BrowserSync(TypedDict):
    startPath: str
    watch: list[str]
    proxy: str
    reload_delay: int
    reload_debounce: int


class SettingsFile(TypedDict):
    """
    Expected format of the settings file.

    This is useful to provide type hints in our editor.
    """

    version: int
    logging: SettingsLog
    browsersync: BrowserSync
