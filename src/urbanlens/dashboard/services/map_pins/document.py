"""One account's map pins as a single streamed document.

The map page used to fetch its pins 500 at a time, which for a 10,000-pin account
is 20 sequential round trips before the last marker appears. This is the same
data in one response.

NDJSON rather than a JSON array: the client can split a `ReadableStream` on
newlines in a few dozen lines, each line is a complete value, and a missing `end`
line detects a truncated response, which a bare array cannot.

The server holds one batch at a time, never the whole account, so response size
does not become worker memory. Accounts above `UL_MAP_DOCUMENT_MAX_PINS` are told
to use the paged endpoint instead.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from django.conf import settings
import redis
from redis.exceptions import RedisError

from urbanlens.dashboard.services.map_pins.fingerprint import pin_collection_state
from urbanlens.dashboard.services.map_pins.payload import PAYLOAD_VERSION, MapPinPayloadService

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

#: Bumped when the line grammar changes, independently of the payload's shape.
FORMAT_VERSION = 1

#: What the endpoint serves, and what the client should do about it.
MODE_DOCUMENT = "document"
MODE_PAGED = "paged"

CONTENT_TYPE = "application/x-ndjson"

#: Pins serialized per database round trip while streaming.
BATCH_SIZE = 1_000

#: Bytes accumulated before a chunk is yielded, so a 20,000-line document is not
#: 20,000 socket writes each with its own chunked-encoding frame. Precautionary:
#: Django's test client consumes the generator directly and never writes to a
#: socket, so the suite cannot measure this either way.
CHUNK_BYTES = 64 * 1024

#: A cache that cannot answer is a miss, never an error. RuntimeError is the test
#: suite's network guard.
_CACHE_ERRORS = (RedisError, ConnectionError, OSError, RuntimeError)

#: How long one account's claim suppresses further build tasks for it. Long
#: enough to cover a build, short enough that a lost one is retried promptly -
#: and it is what bounds the worker time an editing user can cost: at most one
#: rebuild per account per window, however often they edit.
_BUILD_CLAIM_SECONDS = 120


def document_etag(profile: Profile) -> tuple[str, int]:
    """The document's identity, and how many pins it would carry.

    Derived rather than stored: a hash over the pin collection's fingerprint and
    the two version numbers. No writes, so it cannot drift from the data, and
    every path that changes a pin already moves the fingerprint.

    Args:
        profile: Whose map document to identify.

    Returns:
        The ETag value (without quotes) and the profile's root pin count.
    """
    state = pin_collection_state(profile)
    seed = f"{FORMAT_VERSION}:{PAYLOAD_VERSION}:{state.fingerprint}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16], state.total


def stream(
    profile: Profile,
    query: QuerySet[Pin],
    *,
    etag: str,
    total: int,
    decorate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> Iterator[bytes]:
    """Serialize the document one batch at a time.

    Args:
        profile: Whose pins these are.
        query: The profile's root pins, already scoped.
        etag: The identity to declare in the head line.
        total: How many pins the head line should promise.
        decorate: Applied to each batch before encoding, for the request-layer
            fields the payload service does not know about. The paged endpoint
            applies the same function, which is what keeps the two agreeing.

    Yields:
        Chunks of whole NDJSON lines, each line ending in a newline.
    """
    service = MapPinPayloadService(profile)
    # The whole label vocabulary up front, so a pin line can name its labels by
    # id and the client can resolve them the moment it reads one.
    head = {
        "t": "head",
        "mode": MODE_DOCUMENT,
        "total": total,
        "etag": etag,
        "version": FORMAT_VERSION,
        "labels": service.label_dictionary(),
    }
    buffer: list[bytes] = [_line(head)]
    pending = len(buffer[0])

    sent = 0
    cursor: int | None = None
    while True:
        page = service.page(query, cursor=cursor, limit=BATCH_SIZE)
        pins = decorate(page.pins) if decorate else page.pins
        for pin in pins:
            line = _line({"t": "pin", "p": pin})
            buffer.append(line)
            pending += len(line)
            if pending >= CHUNK_BYTES:
                yield b"".join(buffer)
                buffer, pending = [], 0
        sent += len(pins)
        cursor = page.next_cursor
        if cursor is None:
            break

    # The client treats a document without this line as truncated, which is the
    # only way it can tell a short read from a small account.
    buffer.append(_line({"t": "end", "sent": sent}))
    yield b"".join(buffer)


def paged_header(total: int, etag: str) -> bytes:
    """A one-line document telling the client to use the paged endpoint.

    Args:
        total: How many pins the account holds.
        etag: The identity that count belongs to.

    Returns:
        The encoded head line.
    """
    return _line({"t": "head", "mode": MODE_PAGED, "total": total, "etag": etag, "version": FORMAT_VERSION})


def max_pins() -> int:
    """Largest account served as one document.

    Returns:
        The configured ceiling.
    """
    return int(getattr(settings, "MAP_DOCUMENT_MAX_PINS", 30_000))


def _line(value: dict[str, Any]) -> bytes:
    """Encode one NDJSON line.

    Args:
        value: The object to encode.

    Returns:
        Compact JSON followed by a newline.
    """
    return json.dumps(value, separators=(",", ":"), default=str).encode() + b"\n"


def make_binary_client() -> Any:
    """A Valkey client that does not decode what it reads.

    Documents are stored gzipped, so the client must not try to read what it
    gets back as UTF-8.

    Returns:
        The client, or None when no cache is configured.
    """
    url = os.getenv("UL_VALKEY_URL") or os.getenv("UL_REDIS_URL")
    if not url:
        return None
    return redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1, socket_timeout=2)


class MapDocumentCache:
    """Gzipped documents in Valkey, keyed by the content they hold.

    The key contains the ETag, and the ETag is a pure function of the content, so
    two builders racing write identical bytes. There is no lock, no rename and no
    generation flag - the failure modes of the per-pin cache this sits beside
    (P101) have nowhere to live in this shape. A stale entry is never read
    because nothing asks for its key again; it simply expires.
    """

    PREFIX = "ul:map-doc"

    def __init__(self, profile_id: int, client: Any = None) -> None:
        self.profile_id = profile_id
        self.client = client if client is not None else make_binary_client()

    def key(self, etag: str) -> str:
        """Where one version of this profile's document lives.

        Args:
            etag: The document's identity.

        Returns:
            The Valkey key.
        """
        return f"{self.PREFIX}:{FORMAT_VERSION}:{self.profile_id}:{etag}"

    @staticmethod
    def ttl() -> int:
        """How long an entry lives.

        Returns:
            Seconds, or 0 when the cache is disabled.
        """
        return int(getattr(settings, "MAP_DOCUMENT_CACHE_SECONDS", 0))

    @staticmethod
    def _decoded(stored: Any) -> bytes:
        """Normalise whatever the client returned into bytes.

        Args:
            stored: A value read back from the cache.

        Returns:
            The stored bytes.
        """
        return stored.encode("latin-1") if isinstance(stored, str) else bytes(stored)

    def get(self, etag: str) -> bytes | None:
        """The stored document for this exact version, if there is one.

        Args:
            etag: The document's identity.

        Returns:
            Gzipped NDJSON, or None on a miss or any cache failure.
        """
        if not self.client or self.ttl() <= 0:
            return None
        try:
            stored = self.client.get(self.key(etag))
        except _CACHE_ERRORS:
            return None
        if stored is None:
            return None
        return self._decoded(stored)

    def claim_build(self) -> bool:
        """Whether this caller should be the one to enqueue a build.

        Without it every miss enqueues its own task: an account open in several
        tabs schedules several identical multi-megabyte builds, and - because
        every edit makes a new version - one person editing their own map
        schedules one per edit, each of them seconds of worker time on a large
        account and each discarded if the next edit lands while it runs.

        Keyed on the account rather than the version for that reason. Whoever
        claims next builds whatever version is current by then, which is the one
        worth having. The marker expires on its own, so a build that dies simply
        lets the next reader try again.

        Returns:
            True at most once per account per window, and True whenever there is
            no cache to co-ordinate through - one task is better than none.
        """
        if not self.client or self.ttl() <= 0:
            return True
        try:
            return bool(self.client.set(f"{self.PREFIX}:{FORMAT_VERSION}:{self.profile_id}:building", b"1", nx=True, ex=_BUILD_CLAIM_SECONDS))
        except _CACHE_ERRORS:
            return True

    def set(self, etag: str, body: bytes) -> bool:
        """Store a built document, if nobody else already did.

        Args:
            etag: The document's identity.
            body: Gzipped NDJSON.

        Returns:
            Whether this call was the one that stored it.
        """
        if not self.client or self.ttl() <= 0:
            return False
        try:
            return bool(self.client.set(self.key(etag), body, nx=True, ex=self.ttl()))
        except _CACHE_ERRORS:
            return False


def build_and_store(
    profile: Profile,
    query: QuerySet[Pin],
    *,
    decorate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> int:
    """Build a profile's document and cache it.

    Holds the whole document in memory, so it belongs in a worker rather than in
    a request - the streaming path above exists precisely so a request never has
    to.

    Args:
        profile: Whose document to build.
        query: The profile's root pins, already scoped.
        decorate: Applied to each batch, exactly as the request path applies it.

    Returns:
        Bytes stored, or 0 if the document was not stored.
    """
    etag, total = document_etag(profile)
    if total > max_pins():
        return 0
    cache = MapDocumentCache(profile.pk)
    if cache.get(etag) is not None:
        return 0

    body = gzip.compress(b"".join(stream(profile, query, etag=etag, total=total, decorate=decorate)), compresslevel=6)

    # The key is a promise about the content, so a write that happened while this
    # was building would make it a lie. Cheaper to check afterwards and discard
    # than to hold a lock, and the next reader rebuilds.
    if document_etag(profile)[0] != etag:
        return 0
    return len(body) if cache.set(etag, body) else 0
