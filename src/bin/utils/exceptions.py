"""

Metadata:

        File: exceptions.py
        Project: Urban Lens

        Author: Jess Mann
        Email: jess@urbanlens.org

        -----


        Modified By: Jess Mann

        -----

        Copyright (c) 2022 UrbanLens
"""


class AppException(Exception):
    """
    A base exception that all our custom app exceptions extend from.
    """


class FileEmptyError(AppException):
    """
    Raised when a file is empty that is required to have content (i.e. our settings file)
    """


class DbError(AppException):
    """
    Raised when there is a problem with the DB.

    This is inherited by several subclasses.
    """


class DbConnectionError(DbError, ConnectionError):
    """
    Raised when the database cannot be contacted, but it appears to be running.
    """


class DbStartError(DbError, ConnectionError):
    """
    Raised when the database cannot be started.
    """


class UnsupportedCommandError(AppException):
    """
    Raised when a command is passed to our app that isn't valid.
    """


class UnrecoverableError(AppException):
    """
    Raised when an error occurs that is unrecoverable.
    """
