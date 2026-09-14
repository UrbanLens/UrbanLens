from __future__ import annotations

from typing import Literal, Required, TypedDict


class Logger(TypedDict, total=False):
    """Logger entry in the settings file."""

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
    """Logging section; passes straight to dictConfig."""

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
    """Settings file shape (for editor hints)."""

    version: int
    logging: SettingsLog
    browsersync: BrowserSync
