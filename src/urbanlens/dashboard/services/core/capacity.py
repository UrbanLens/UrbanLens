"""Per-owner row limits configured in ``SiteSettings``.

Each limit names the ``SiteSettings`` field that sets it (``0`` means unlimited) and the rows that count
against it. Every write path that adds such a row - form, API, import, undo - goes through
:func:`reserve`, which serialises that owner's adds so two concurrent requests cannot both see room
for one more.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.db import connection, transaction

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capacity:
    """One configurable ceiling on how many rows an owner may hold.

    Attributes:
        setting: The ``SiteSettings`` integer field holding the limit; ``0`` means unlimited.
        noun: Plural, user-facing name of what is counted.
        container: User-facing name of what holds the rows, for messages.
        unlimited_ceiling: The field's own validator maximum, used where a finite bound is still needed
            (truncating a posted id list) while the setting is unlimited.
        in_use: The rows counted against an owner, given the owner's primary key.
    """

    setting: str
    noun: str
    container: str
    unlimited_ceiling: int
    in_use: Callable[[int], QuerySet[Any]]

    def limit(self) -> int | None:
        """The configured limit, or None when unlimited.

        Returns:
            The positive limit, or None.
        """
        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        configured = int(getattr(SiteSettings.get_current(), self.setting))
        return configured if configured > 0 else None

    def ceiling(self) -> int:
        """A finite bound: the limit, or the setting's maximum when unlimited.

        Returns:
            A positive ceiling.
        """
        return self.limit() or self.unlimited_ceiling


class CapacityExceededError(Exception):
    """Adding the rows would take the owner past a configured limit."""

    def __init__(self, capacity: Capacity, limit: int, in_use: int, adding: int) -> None:
        self.capacity = capacity
        self.limit = limit
        self.in_use = in_use
        self.adding = adding
        super().__init__(f"{capacity.setting}: {in_use} in use + {adding} > {limit}")

    @property
    def user_message(self) -> str:
        """Why the add was refused, for the person who asked.

        Returns:
            A sentence naming the limit and, when some room remains, how much.
        """
        remaining = max(0, self.limit - self.in_use)
        if remaining and self.adding > 1:
            return f"{self.capacity.container} can hold {self.limit} {self.capacity.noun}; there is room for {remaining} more."
        return f"{self.capacity.container} already has the maximum of {self.limit} {self.capacity.noun}."


@contextmanager
def reserve(capacity: Capacity, owner_pk: int, *, adding: int = 1, in_use: QuerySet[Any] | None = None) -> Iterator[None]:
    """Hold room for *adding* rows under *owner_pk* while the body inserts them.

    Opens a transaction, serialises this owner's adds for this limit on an advisory lock, and counts.
    The insert must happen inside the ``with`` block for the count to still be true when it commits.

    Args:
        capacity: The limit being reserved against.
        owner_pk: Primary key of the owner (a profile, an album).
        adding: How many rows the body will add.
        in_use: The counted rows, when the caller's differ from ``capacity.in_use`` (e.g. excluding
            a row the body will revive rather than add).

    Yields:
        Nothing; the body runs inside the transaction and lock.

    Raises:
        CapacityExceededError: The owner has no room for *adding* more rows.
    """
    with transaction.atomic():
        limit = capacity.limit()
        if limit is not None and adding > 0:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"capacity:{capacity.setting}:{owner_pk}"])
            counted = (in_use if in_use is not None else capacity.in_use(owner_pk)).count()
            if counted + adding > limit:
                logger.info("Capacity %s refused for owner %s: %d in use, adding %d, limit %d", capacity.setting, owner_pk, counted, adding, limit)
                raise CapacityExceededError(capacity, limit, counted, adding)
        yield


@contextmanager
def reserve_each(capacity: Capacity, adding_by_owner: Mapping[int, int]) -> Iterator[None]:
    """:func:`reserve` for several owners at once, locking in key order so two batches cannot deadlock.

    Args:
        capacity: The limit being reserved against.
        adding_by_owner: How many rows the body adds per owner primary key.

    Yields:
        Nothing; the body runs inside the transaction and every owner's lock.

    Raises:
        CapacityExceededError: Any one owner has no room.
    """
    with transaction.atomic(), ExitStack() as stack:
        for owner_pk in sorted(adding_by_owner):
            stack.enter_context(reserve(capacity, owner_pk, adding=adding_by_owner[owner_pk]))
        yield


def ensure_room(capacity: Capacity, owner_pk: int, *, adding: int = 1) -> None:
    """Refuse early, before expensive work such as an upload, when the owner is already full.

    Takes no lock, so it only saves wasted work; the insert itself must still go through :func:`reserve`.

    Args:
        capacity: The limit being checked.
        owner_pk: Primary key of the owner.
        adding: How many rows the caller is about to add.

    Raises:
        CapacityExceededError: The owner has no room for *adding* more rows.
    """
    limit = capacity.limit()
    if limit is None:
        return
    counted = capacity.in_use(owner_pk).count()
    if counted + adding > limit:
        raise CapacityExceededError(capacity, limit, counted, adding)


def _saved_filters(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

    return SavedFilter.objects.filter(profile_id=owner_pk)


def _push_devices(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.push_device.model import PushDevice

    return PushDevice.objects.filter(profile_id=owner_pk).active()


def _custom_fields(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.custom_fields.model import CustomField

    return CustomField.objects.filter(profile_id=owner_pk)


def _labels(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.labels.model import Label

    return Label.objects.filter(profile_id=owner_pk)


def _pin_lists(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.pin_list.model import PinList

    return PinList.objects.filter(profile_id=owner_pk)


def _album_photos(owner_pk: int) -> QuerySet[Any]:
    from urbanlens.dashboard.models.album.model import AlbumItem

    return AlbumItem.objects.filter(album_id=owner_pk)


SAVED_FILTERS = Capacity("max_saved_filters_per_user", "saved filters", "Your account", 10_000, _saved_filters)
PUSH_DEVICES = Capacity("max_push_devices_per_user", "push devices", "Your account", 1_000, _push_devices)
CUSTOM_FIELDS = Capacity("max_custom_fields_per_user", "custom fields", "Your account", 10_000, _custom_fields)
LABELS = Capacity("max_labels_per_user", "labels", "Your account", 100_000, _labels)
PIN_LISTS = Capacity("max_pin_lists_per_user", "lists", "Your account", 100_000, _pin_lists)
ALBUM_PHOTOS = Capacity("max_photos_per_album", "photos", "This album", 1_000_000, _album_photos)
