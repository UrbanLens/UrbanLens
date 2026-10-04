"""Background imports of photos picked from a connected library or a public album, and how they wait out storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
import io
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.files.base import ContentFile

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.media.storage_errors import IMPORT_STORAGE_WAITS, STORAGE_ERRORS, storage_retry_countdown

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from celery import Task

    from urbanlens.dashboard.models.flickr.model import FlickrAccount
    from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
    from urbanlens.dashboard.models.immich.model import ImmichAccount
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.apis.flickr.public import FlickrAlbum, FlickrPublicGateway

logger = logging.getLogger(__name__)


class _StorageRefusedError(Exception):
    """Storage refused a photo's write; the storage error is its cause."""


@dataclass(slots=True)
class ImportCounts:
    """What a background photo import did with the photos it was given, across every retry.

    Attributes:
        imported: Photos stored.
        skipped: Photos already imported onto the same target.
        failed: Photos that could not be fetched, or that the quota or the upload reservation refused.
        storage_unavailable: Photos left unimported because media storage stayed unavailable.
    """

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    storage_unavailable: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ImportCounts:
        """Read counts back from a task's arguments or result.

        Args:
            data: A mapping from :meth:`as_dict`, or None.

        Returns:
            The counts, zero for anything missing.
        """
        data = data if isinstance(data, dict) else {}
        return cls(**{item.name: int(data.get(item.name) or 0) for item in fields(cls)})

    def as_dict(self) -> dict[str, int]:
        """The counts as a JSON-safe task argument or result."""
        return asdict(self)

    @property
    def settled(self) -> int:
        """Photos the import is finished with, whatever became of them."""
        return self.imported + self.skipped + self.failed + self.storage_unavailable

    def summary(self) -> str:
        """One line saying what became of the selection.

        Returns:
            The summary, for the progress dialog and its toast.
        """
        parts = [f"Imported {self.imported} photo(s)"]
        if self.skipped:
            parts.append(f"skipped {self.skipped} duplicate(s)")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.storage_unavailable:
            parts.append(f"{self.storage_unavailable} not imported because storage was unavailable; try those again later")
        return ", ".join(parts) + "."

    def toast(self) -> dict[str, str]:
        """The ``showToast`` payload for a finished import.

        Returns:
            A warning when any photo was not imported, else a success.
        """
        level = "warning" if self.failed or self.storage_unavailable else "success"
        return {"level": level, "message": self.summary()}


class PhotoImport(ABC):
    """One background import of photos picked from a single source, stored one at a time onto a pin or wiki.

    A photo storage refuses ends the run, and the task is retried later for the photos left, so a photo already
    stored is neither downloaded nor stored again. A refusal with nothing stored since the last one waits longer, up
    to ``IMPORT_STORAGE_WAITS`` waits; the photos still left then count as not imported.

    Attributes:
        source: The ``ImageSource`` the stored rows carry.
    """

    source: ClassVar[str]

    def __init__(self, task: Task, profile: Profile, *, done: Mapping[str, Any] | None = None, storage_waits: int = 0) -> None:
        """Start, or resume, an import.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        self.task = task
        self.profile = profile
        self.counts = ImportCounts.from_dict(done)
        self.storage_waits = storage_waits

    @property
    def name(self) -> str:
        """Names the import in log lines."""
        return type(self).__name__

    @abstractmethod
    def dedupe_filter(self) -> dict[str, Any]:
        """Lookups matching a photo already imported onto the same target, alongside its checksum.

        Returns:
            ``Image`` filter keyword arguments.
        """

    @abstractmethod
    def download(self, item_id: str) -> tuple[bytes, str]:
        """Fetch one picked photo.

        Args:
            item_id: The source's id for it.

        Returns:
            The bytes and a filename.

        Raises:
            GatewayRequestError: It could not be fetched.
        """

    @abstractmethod
    def build(self, item_id: str, content: bytes, filename: str, checksum: str) -> Image:
        """The unsaved row for one photo, its file set and ``pending_scan`` on.

        Args:
            item_id: The source's id for it.
            content: Its bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.

        Returns:
            The row, ready to insert.
        """

    @abstractmethod
    def call_for(self, remaining: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """The task's arguments for importing just *remaining*.

        Args:
            remaining: The ids left, in order.

        Returns:
            Positional and keyword arguments.
        """

    @abstractmethod
    def stored(self, image: Image) -> None:
        """Run once a photo's row is committed, before it is queued for processing.

        Args:
            image: The stored row.
        """

    def run(self, item_ids: Sequence[str]) -> dict[str, int]:
        """Import *item_ids* in order.

        Args:
            item_ids: The picked ids this run imports.

        Returns:
            The counts for the whole selection, the earlier runs' included.

        Raises:
            celery.exceptions.Retry: Storage refused a photo and the import will wait for it.
        """
        from urbanlens.dashboard.services.core.celery import update_task_progress

        offset = self.counts.settled
        total = offset + len(item_ids)
        progressed = False
        for index, item_id in enumerate(item_ids):
            update_task_progress(self.task, current=offset + index, total=total, message=f"Importing photo {offset + index + 1} of {total}...")
            try:
                progressed = self._import(item_id) or progressed
            except _StorageRefusedError as refused:
                self._wait_for_storage(refused.__cause__ or refused, list(item_ids[index:]), progressed=progressed, total=total)
                break
        update_task_progress(self.task, current=total, total=total, message=self.counts.summary())
        return self.counts.as_dict()

    def _import(self, item_id: str) -> bool:
        """Download, dedupe and store one photo.

        Args:
            item_id: The source's id for it.

        Returns:
            Whether it was stored.

        Raises:
            _StorageRefusedError: Storage refused the write; nothing was stored.
        """
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.services.media.images import compute_checksum
        from urbanlens.dashboard.services.media.storage import BACKGROUND_RESERVATION_WAIT_SECONDS, UploadRefusedError, reserve_upload
        from urbanlens.dashboard.tasks import process_image_upload

        try:
            content, filename = self.download(item_id)
        except GatewayRequestError:
            logger.warning("%s: could not fetch %s", self.name, item_id, exc_info=True)
            self.counts.failed += 1
            return False

        checksum = compute_checksum(io.BytesIO(content))
        try:
            with reserve_upload(self.profile, None, wait_seconds=BACKGROUND_RESERVATION_WAIT_SECONDS) as reservation:
                if Image.objects.filter(checksum=checksum, **self.dedupe_filter()).exists():
                    self.counts.skipped += 1
                    return False
                reservation.reserve(len(content))
                image = self.build(item_id, content, filename, checksum)
                image.save(force_insert=True)
        except UploadRefusedError:
            self.counts.failed += 1
            return False
        except STORAGE_ERRORS as exc:
            raise _StorageRefusedError from exc
        self.stored(image)
        safely_enqueue_task(process_image_upload, image.pk)
        self.counts.imported += 1
        return True

    def _wait_for_storage(self, exc: BaseException, remaining: list[str], *, progressed: bool, total: int) -> None:
        """Retry the task for *remaining* once storage has had time to recover, or give up on them.

        Args:
            exc: What storage raised.
            remaining: The ids left, the refused one first.
            progressed: Whether this run stored a photo before the refusal.
            total: Photos in the whole selection.

        Raises:
            celery.exceptions.Retry: While waits are left.
        """
        from urbanlens.dashboard.services.core.celery import RetryNoticeError
        from urbanlens.dashboard.services.media.upload_retry import report_lasting_refusal

        report_lasting_refusal(exc)
        waits = 0 if progressed else self.storage_waits
        if waits >= IMPORT_STORAGE_WAITS:
            logger.warning("%s: storage refused %d time(s) in a row, so %d photo(s) were not imported", self.name, waits + 1, len(remaining), exc_info=exc)
            self.counts.storage_unavailable += len(remaining)
            return
        countdown = storage_retry_countdown(waits)
        logger.warning("%s: storage refused a photo; the %d left resume in %ss", self.name, len(remaining), countdown, exc_info=exc)
        notice = RetryNoticeError(
            f"Storage is briefly unavailable. Imported {self.counts.imported} of {total} so far; trying the rest again in {countdown // 60} minute(s)...",
            self.counts.settled,
            total,
        )
        args, kwargs = self.call_for(remaining)
        raise self.task.retry(args=args, kwargs={**kwargs, "done": self.counts.as_dict(), "storage_waits": waits + 1}, countdown=countdown, exc=notice)


class _PinPhotoImport(PhotoImport):
    """An import from one account's own library onto that account's pin, logging a visit per photo."""

    def __init__(self, task: Task, profile: Profile, pin: Pin, *, done: Mapping[str, Any] | None = None, storage_waits: int = 0) -> None:
        """Start, or resume, an import onto *pin*.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile, the pin's owner.
            pin: The pin to import onto.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        super().__init__(task, profile, done=done, storage_waits=storage_waits)
        self.pin = pin

    def dedupe_filter(self) -> dict[str, Any]:
        """The same photo on the same pin by the same profile.

        Returns:
            ``Image`` filter keyword arguments.
        """
        return {"pin": self.pin, "profile": self.profile}

    def row(self, content: bytes, filename: str, checksum: str, source_url: str, **extra: Any) -> Image:
        """A pending row on the pin.

        Args:
            content: The photo's bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.
            source_url: Its page at the source.
            **extra: Further ``Image`` fields.

        Returns:
            The unsaved row.
        """
        # Third-party bytes, stored unread: invisible to others until process_image_upload clears pending_scan.
        return Image(
            image=ContentFile(content, name=filename),
            pin=self.pin,
            location=self.pin.location,
            profile=self.profile,
            source=self.source,
            checksum=checksum,
            file_size=len(content),
            source_url=source_url,
            pending_scan=True,
            **extra,
        )

    def stored(self, image: Image) -> None:
        """Log a visit on the pin for the photo, unless it already joined one.

        Args:
            image: The stored row.
        """
        from urbanlens.dashboard.services.memories.photos import log_visit_on_pin

        if image.visit_id is None:
            log_visit_on_pin(self.profile, image, self.pin)


class ImmichPhotoImport(_PinPhotoImport):
    """Selected Immich assets onto a pin."""

    source = ImageSource.IMMICH

    def __init__(
        self,
        task: Task,
        profile: Profile,
        pin: Pin,
        account: ImmichAccount,
        visit_id_by_asset: Mapping[str, int] | None = None,
        *,
        done: Mapping[str, Any] | None = None,
        storage_waits: int = 0,
    ) -> None:
        """Start, or resume, an Immich import.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile, the pin's owner.
            pin: The pin to import onto.
            account: The profile's Immich connection.
            visit_id_by_asset: Asset id -> the visit on *pin* its photo joins, for an accepted pin suggestion.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        from urbanlens.dashboard.services.apis.immich import ImmichGateway

        super().__init__(task, profile, pin, done=done, storage_waits=storage_waits)
        self.account = account
        self.gateway = ImmichGateway(account=account)
        self.visit_id_by_asset = dict(visit_id_by_asset or {})

    def download(self, item_id: str) -> tuple[bytes, str]:
        """Fetch an asset's original.

        Args:
            item_id: The Immich asset id.

        Returns:
            The bytes and filename.
        """
        content, filename, _content_type = self.gateway.get_asset_original(item_id)
        return content, filename

    def build(self, item_id: str, content: bytes, filename: str, checksum: str) -> Image:
        """The asset's row, joined to its suggested visit when it has one on this pin.

        Args:
            item_id: The Immich asset id.
            content: Its bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.

        Returns:
            The unsaved row.
        """
        from urbanlens.dashboard.models.visits.model import PinVisit

        visit_id = self.visit_id_by_asset.get(item_id)
        visit = PinVisit.objects.filter(pk=visit_id, pin=self.pin).first() if visit_id else None
        return self.row(content, filename, checksum, self.account.asset_web_url(item_id), visit=visit)

    def call_for(self, remaining: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """``import_immich_photos``'s arguments for *remaining*.

        Args:
            remaining: The asset ids left.

        Returns:
            Positional and keyword arguments.
        """
        return (self.pin.pk, self.profile.pk, remaining), {"visit_id_by_asset": self.visit_id_by_asset or None}


class FlickrPhotoImport(_PinPhotoImport):
    """Selected photos from the profile's own Flickr library onto a pin."""

    source = ImageSource.FLICKR

    def __init__(self, task: Task, profile: Profile, pin: Pin, account: FlickrAccount, *, done: Mapping[str, Any] | None = None, storage_waits: int = 0) -> None:
        """Start, or resume, a Flickr import.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile, the pin's owner.
            pin: The pin to import onto.
            account: The profile's Flickr connection.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway

        super().__init__(task, profile, pin, done=done, storage_waits=storage_waits)
        self.account = account
        self.gateway = FlickrGateway(account=account)

    def download(self, item_id: str) -> tuple[bytes, str]:
        """Fetch a photo's original.

        Args:
            item_id: The Flickr photo id.

        Returns:
            The bytes and filename.
        """
        content, filename, _content_type = self.gateway.get_original(item_id)
        return content, filename

    def build(self, item_id: str, content: bytes, filename: str, checksum: str) -> Image:
        """The photo's row.

        Args:
            item_id: The Flickr photo id.
            content: Its bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.

        Returns:
            The unsaved row.
        """
        return self.row(content, filename, checksum, self.account.photo_web_url(item_id))

    def call_for(self, remaining: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """``import_flickr_photos``'s arguments for *remaining*.

        Args:
            remaining: The photo ids left.

        Returns:
            Positional and keyword arguments.
        """
        return (self.pin.pk, self.profile.pk, remaining), {}


class GooglePhotosImport(_PinPhotoImport):
    """Items picked in a Google Photos picker session onto a pin."""

    source = ImageSource.GOOGLE_PHOTOS

    def __init__(
        self,
        task: Task,
        profile: Profile,
        pin: Pin,
        account: GooglePhotosAccount,
        session_id: str,
        item_ids: Sequence[str],
        *,
        done: Mapping[str, Any] | None = None,
        storage_waits: int = 0,
    ) -> None:
        """Start, or resume, a Google Photos import, resolving each item's download address.

        The session's items are read from the cache the picker filled; the session is listed again only for an item
        missing from it.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile, the pin's owner.
            pin: The pin to import onto.
            account: The profile's Google Photos connection.
            session_id: The picker session the items were picked in.
            item_ids: The items this run imports.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        from django.core.cache import cache

        from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway, session_items_cache_key

        super().__init__(task, profile, pin, done=done, storage_waits=storage_waits)
        self.session_id = session_id
        self.gateway = GooglePhotosGateway(account=account)
        self.items: dict[str, dict[str, str]] = cache.get(session_items_cache_key(session_id)) or {}
        missing = [item_id for item_id in item_ids if item_id not in self.items]
        if missing:
            try:
                for item in self.gateway.list_session_media_items(session_id):
                    self.items[item.id] = {"base_url": item.base_url, "mime_type": item.mime_type, "filename": item.filename}
            except GatewayRequestError:
                logger.warning("import_google_photos: could not re-list session %s to resolve %d missing item(s)", session_id, len(missing), exc_info=True)

    def download(self, item_id: str) -> tuple[bytes, str]:
        """Fetch an item's original.

        Args:
            item_id: The picker media item id.

        Returns:
            The bytes and filename.

        Raises:
            GatewayRequestError: The session does not list the item, or the download failed.
        """
        item = self.items.get(item_id)
        if item is None:
            raise GatewayRequestError(f"Google Photos picker session {self.session_id} does not list item {item_id}")
        return self.gateway.download_media_item(item["base_url"], original=True), item.get("filename") or f"{item_id}.jpg"

    def build(self, item_id: str, content: bytes, filename: str, checksum: str) -> Image:
        """The item's row.

        Args:
            item_id: The picker media item id.
            content: Its bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.

        Returns:
            The unsaved row.
        """
        from urbanlens.dashboard.services.apis.photos.google import media_item_web_url

        return self.row(content, filename, checksum, media_item_web_url(item_id))

    def call_for(self, remaining: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """``import_google_photos``'s arguments for *remaining*.

        Args:
            remaining: The item ids left.

        Returns:
            Positional and keyword arguments.
        """
        return (self.pin.pk, self.profile.pk, self.session_id, remaining), {}


class FlickrAlbumPhotoImport(PhotoImport):
    """Selected photos from a public Flickr album onto a pin or wiki, with the photographer credited."""

    source = ImageSource.FLICKR

    def __init__(
        self,
        task: Task,
        profile: Profile,
        *,
        pin: Pin | None,
        wiki: Wiki | None,
        location: Location,
        album_url: str,
        album: FlickrAlbum,
        gateway: FlickrPublicGateway,
        done: Mapping[str, Any] | None = None,
        storage_waits: int = 0,
    ) -> None:
        """Start, or resume, an album import.

        Args:
            task: The bound Celery task running it.
            profile: The importing profile.
            pin: The pin to import onto, or None for a wiki.
            wiki: The wiki to import onto, or None for a pin.
            location: The target's location.
            album_url: The album's URL, as submitted.
            album: The album, resolved again from its URL.
            gateway: The client that resolved it.
            done: Counts carried from the runs before this one.
            storage_waits: Waits for storage in a row so far.
        """
        super().__init__(task, profile, done=done, storage_waits=storage_waits)
        self.pin = pin
        self.wiki = wiki
        self.location = location
        self.album_url = album_url
        self.album = album
        self.gateway = gateway
        self.photos_by_id = {photo.id: photo for photo in album.photos}

    def dedupe_filter(self) -> dict[str, Any]:
        """The same photo on the same pin or wiki by the same profile.

        Returns:
            ``Image`` filter keyword arguments.
        """
        return {"profile": self.profile, **({"pin": self.pin} if self.pin is not None else {"wiki": self.wiki})}

    def download(self, item_id: str) -> tuple[bytes, str]:
        """Fetch an album photo.

        Args:
            item_id: The Flickr photo id, one the album lists.

        Returns:
            The bytes and filename.
        """
        content, filename, _content_type = self.gateway.download_photo(self.photos_by_id[item_id])
        return content, filename

    def build(self, item_id: str, content: bytes, filename: str, checksum: str) -> Image:
        """The photo's row, credited to its photographer.

        Args:
            item_id: The Flickr photo id.
            content: Its bytes.
            filename: Its filename.
            checksum: The bytes' SHA-256.

        Returns:
            The unsaved row.
        """
        from urbanlens.dashboard.services.apis.flickr.public import photo_web_url

        photo = self.photos_by_id[item_id]
        # Third-party bytes, stored unread: invisible to others until process_image_upload clears pending_scan.
        return Image(
            image=ContentFile(content, name=filename),
            pin=self.pin,
            wiki=self.wiki,
            location=self.location,
            profile=self.profile,
            source=self.source,
            caption=photo.title or "",
            author=photo.author,
            source_url=photo_web_url(self.album.owner_nsid, photo.id),
            checksum=checksum,
            file_size=len(content),
            pending_scan=True,
        )

    def stored(self, image: Image) -> None:
        """Log nothing: an album is somebody else's photos, so they say nothing of the importer's visits.

        Args:
            image: The stored row.
        """
        del image

    def call_for(self, remaining: list[str]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """``import_flickr_album_photos``'s arguments for *remaining*.

        Args:
            remaining: The photo ids left.

        Returns:
            Positional and keyword arguments.
        """
        target_kind, target_id = ("pin", self.pin.pk) if self.pin is not None else ("wiki", self.wiki.pk if self.wiki is not None else 0)
        return (target_kind, target_id, self.profile.pk, self.album_url, remaining), {}
