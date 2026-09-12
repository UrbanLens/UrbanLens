class AppError(Exception):
    """Base app exception."""


class FileEmptyError(AppError):
    """File required to have content is empty."""


class DbError(AppError):
    """Database problem (base for subclasses)."""


class DbConnectionError(DbError, ConnectionError):
    """Database unreachable but apparently running."""


class DbStartError(DbError, ConnectionError):
    """Database cannot be started."""


class UnsupportedCommandError(AppError):
    """Invalid command passed to the app."""


class UnrecoverableError(AppError):
    """Unrecoverable error."""
