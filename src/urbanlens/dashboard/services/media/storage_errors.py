"""What media storage raises when it cannot do its job, on either backend."""

from __future__ import annotations

from boto3.exceptions import S3UploadFailedError as Boto3UploadFailedError
from botocore.exceptions import ClientError, ConnectionError as BotocoreConnectionError, FlexibleChecksumError, HTTPClientError
from s3transfer.exceptions import RetriesExceededError, S3UploadFailedError as TransferUploadFailedError

__all__ = ["OBJECT_STORE_ERRORS", "STORAGE_ERRORS", "STORAGE_RETRY_AFTER_SECONDS", "is_transient"]

#: The S3 backend's failures: the object store unreachable, timing out, refusing, breaking every download attempt, or
#: sending a download that fails its checksum. The rest of botocore's errors, such as missing credentials or a bad
#: parameter, are misconfiguration and are not here.
OBJECT_STORE_ERRORS: tuple[type[Exception], ...] = (
    BotocoreConnectionError,
    HTTPClientError,
    ClientError,
    RetriesExceededError,
    FlexibleChecksumError,
    Boto3UploadFailedError,
    TransferUploadFailedError,
)

#: Any storage failure: the filesystem backend's OSError, or one of :data:`OBJECT_STORE_ERRORS`.
STORAGE_ERRORS: tuple[type[Exception], ...] = (OSError, *OBJECT_STORE_ERRORS)

#: What a request refused because storage failed tells its client to wait before sending the upload again.
STORAGE_RETRY_AFTER_SECONDS = 30

_TRANSIENT = (BotocoreConnectionError, HTTPClientError, RetriesExceededError, TimeoutError, ConnectionError)


def is_transient(exc: BaseException) -> bool:
    """Whether a storage failure is the store being briefly unreachable or overloaded, rather than refusing for good.

    A refusal that lasts, such as an access-denied key or a full disk, needs an operator, so it is logged louder.

    Args:
        exc: What storage raised.

    Returns:
        True for a timeout, a lost connection or a 5xx answer.
    """
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, ClientError):
        return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
    if isinstance(exc, (Boto3UploadFailedError, TransferUploadFailedError)):
        return exc.__context__ is not None and is_transient(exc.__context__)
    return False
