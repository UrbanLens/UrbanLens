"""Creating a photo from a browser upload, for a Pin, a Wiki, or a Vault.

Four surfaces turn an uploaded file into an :class:`Image`: the pin gallery,
the wiki gallery, an album's own upload button (pin, wiki, or Vault), and the
Vault Photos page's own dropzone. They must agree on every gate an upload
passes - file-type/malware validation, the per-uploader duplicate check, and
the storage quota - so those gates live here rather than being restated per
view. A surface that skips one would let a file in that the others reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import Image, MediaKind, QuotaExemption
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.text_limits import column_length_error
from urbanlens.dashboard.services.media.images import compute_checksum, image_upload_error, prepare_photo_upload
from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki


@dataclass(frozen=True, slots=True)
class UploadRejection:
    """A refused upload, carrying the message and status the caller should return.

    Attributes:
        message: User-facing explanation, safe to show in a toast.
        status: HTTP status describing the refusal (400/409/413/415).
    """

    message: str
    status: int


def _owner_fields(owner: Pin | Wiki | Profile) -> dict:
    """Return the Image ownership FKs for *owner*.

    A pin photo belongs to the pin and to its location. It is **not** attached to
    the location's wiki: everything on a wiki is there because somebody put it
    there. This used to stamp the wiki as well, which made "send to wiki" a no-op
    for anything uploaded after the wiki existed - and meant a photo of your own
    house appeared in that place's community Photos panel, and became votable
    there, without you choosing to share it. The visibility gate narrowed who saw
    it (``photo_upload_visibility`` defaults to "anything in common", and having a
    pin at the same place is a thing in common); it did not make it deliberate.

    A wiki photo has no pin. A Vault (Profile-owned album) upload has neither -
    it's unfiled, exactly like any other photo uploaded straight to the Vault
    gallery rather than to a place.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) the photo is being uploaded to.

    Returns:
        Kwargs for ``Image.objects.create``.
    """
    if isinstance(owner, Pin):
        return {"pin": owner, "location": owner.location}
    if isinstance(owner, Profile):
        return {}
    return {"wiki": owner, "location": owner.location}


def existing_photo_for_upload(
    owner: Pin | Wiki | Profile,
    profile: Profile,
    image_file: UploadedFile | None = None,
    *,
    checksum: str | None = None,
) -> Image | None:
    """Return this uploader's existing photo of the same file on *owner*, if any.

    The duplicate gate is per (owner, uploader, checksum): the same bytes on
    this pin/wiki by this person. Album upload uses this to file the existing
    row instead of treating a 409 as a hard failure.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) the upload was aimed at.
        profile: The uploading profile.
        image_file: The uploaded file, hashed when *checksum* is omitted.
        checksum: Pre-computed SHA-256 hex digest of *image_file*.

    Returns:
        The existing :class:`Image`, or None when this file is new here.
    """
    if checksum is None:
        if image_file is None:
            return None
        checksum = compute_checksum(image_file)
    scope, _noun = _duplicate_scope(owner)
    return Image.objects.filter(profile=profile, checksum=checksum, **scope).first()


def existing_photo_for_profile(profile: Profile, checksum: str) -> Image | None:
    """Return this uploader's existing row of the same file, on any pin or wiki.

    Same bytes and the same person: a second upload should reuse the stored
    file rather than charge quota twice. Different bytes (an edited export
    with the same filename) hash differently and are a new photo.

    Args:
        profile: The uploading profile.
        checksum: SHA-256 hex digest of the file.

    Returns:
        An existing :class:`Image` with a stored file, or None.
    """
    return Image.objects.filter(profile=profile, checksum=checksum).exclude(image="").order_by("pk").first()


_METADATA_FIELDS = ("caption", "author", "copyright", "taken_at", "latitude", "longitude")


def _serialize_meta(value) -> str:
    """JSON-safe string for a metadata conflict choice."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _queue_metadata_conflict(existing: Image, new_row: Image, incoming_caption: str) -> None:
    """Record fields that both copies set and that disagree, for Memories review."""
    from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoMetadataConflict

    conflicts: dict[str, list[str]] = {}
    incoming = incoming_caption.strip()
    existing_caption = (existing.caption or "").strip()
    if incoming and existing_caption and incoming != existing_caption:
        conflicts["caption"] = [existing_caption, incoming]
    for field in _METADATA_FIELDS:
        if field == "caption":
            continue
        left, right = getattr(existing, field), getattr(new_row, field)
        if left in (None, "") or right in (None, ""):
            continue
        if left != right:
            conflicts[field] = [_serialize_meta(left), _serialize_meta(right)]
    if not conflicts:
        return
    PhotoMetadataConflict.objects.create(
        profile=existing.profile,
        existing_image=existing,
        new_image=new_row,
        fields=conflicts,
        status=PhotoIssueStatus.PENDING,
    )


def attach_deduped_copy(existing: Image, owner: Pin | Wiki | Profile, profile: Profile, caption: str) -> Image:
    """Create a new Image row that reuses *existing*'s stored file.

    Does not copy the bytes, does not charge quota, and does not re-run
    ``process_image_upload`` (that would rewrite the shared file). Metadata
    is copied from *existing*; an incoming caption that disagrees is kept on
    the new row and queued for the owner to pick.

    Args:
        existing: The earlier row with the same checksum.
        owner: The Pin, Wiki, or Profile (Vault) this upload is aimed at.
        profile: The uploading profile.
        caption: Caption from this upload, if any.

    Returns:
        The new :class:`Image` row.
    """
    incoming = caption.strip()
    row = Image(
        image=existing.image.name,
        thumbnail=existing.thumbnail.name if existing.thumbnail and existing.thumbnail.name else None,
        marker_thumbnail=existing.marker_thumbnail.name if existing.marker_thumbnail and existing.marker_thumbnail.name else None,
        profile=profile,
        caption=incoming or existing.caption,
        checksum=existing.checksum,
        file_size=existing.file_size,
        quota_exempt_reason=QuotaExemption.DEDUPLICATED,
        author=existing.author,
        source_url=existing.source_url,
        copyright=existing.copyright,
        taken_at=existing.taken_at,
        original_filename=existing.original_filename,
        filename_taken_at=existing.filename_taken_at,
        latitude=existing.latitude,
        longitude=existing.longitude,
        direction=existing.direction,
        exif_data=existing.exif_data,
        source=existing.source,
        media_type=existing.media_type,
        map_hidden=existing.map_hidden,
        **_owner_fields(owner),
    )
    row.save()
    _queue_metadata_conflict(existing, row, incoming)
    return row


def record_photo_upload_failure(
    profile: Profile,
    filename: str,
    error: str,
    *,
    pin: Pin | None = None,
    album=None,
) -> None:
    """Persist a failed upload so Memories can show it for retry.

    Args:
        profile: The uploader.
        filename: Original file name.
        error: User-facing explanation.
        pin: Pin they were uploading to, if any.
        album: Album they were uploading into, if any.
    """
    from urbanlens.dashboard.models.images.issues import PhotoUploadFailure

    PhotoUploadFailure.objects.create(profile=profile, filename=filename[:255], error=error, pin=pin, album=album)


def resolve_photo_metadata_conflict(conflict, choices: dict[str, int]) -> int:
    """Apply the owner's field picks to every copy of this file.

    Args:
        conflict: A pending :class:`PhotoMetadataConflict`.
        choices: Mapping of field name to ``0`` (keep the earlier value) or
            ``1`` (use the later upload's value).

    Returns:
        How many ``Image`` rows were updated.
    """
    from decimal import Decimal

    from django.utils.dateparse import parse_datetime

    from urbanlens.dashboard.models.images.issues import PhotoIssueStatus
    from urbanlens.dashboard.models.images.model import Image

    updates: dict = {}
    for field, pair in (conflict.fields or {}).items():
        if field not in choices or not isinstance(pair, list) or len(pair) < 2:
            continue
        try:
            idx = int(choices[field])
        except (TypeError, ValueError):
            continue
        if idx not in (0, 1):
            continue
        raw = pair[idx]
        if field in ("latitude", "longitude"):
            updates[field] = Decimal(str(raw)) if raw not in ("", None) else None
        elif field == "taken_at":
            updates[field] = parse_datetime(str(raw)) if raw else None
        else:
            updates[field] = raw or None
    checksum = conflict.existing_image.checksum
    qs = Image.objects.filter(profile_id=conflict.profile_id)
    if checksum:
        qs = qs.filter(checksum=checksum)
    else:
        qs = qs.filter(pk__in=[conflict.existing_image_id, conflict.new_image_id])
    count = qs.update(**updates) if updates else 0
    conflict.status = PhotoIssueStatus.RESOLVED
    conflict.save(update_fields=["status", "updated"])
    return count


def _duplicate_scope(owner: Pin | Wiki | Profile) -> tuple[dict, str]:
    """Return the filter isolating *owner*'s photos, and the noun for the error.

    A Vault (Profile) owner's "own photos" are its unfiled ones - the same
    bytes already filed to a pin or wiki are a different, legitimate copy to
    also keep unfiled in the Vault, not a duplicate of it.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) being uploaded to.

    Returns:
        Tuple of (queryset filter kwargs, the word to use in "already uploaded
        this photo to this <noun>").
    """
    if isinstance(owner, Pin):
        return {"pin": owner}, "pin"
    if isinstance(owner, Profile):
        return {"pin__isnull": True, "wiki__isnull": True}, "vault"
    return {"wiki": owner}, "wiki"


def upload_photo_for_owner(owner: Pin | Wiki | Profile, profile: Profile, image_file: UploadedFile, caption: str = "") -> Image | UploadRejection:
    """Validate and store one uploaded photo against *owner*.

    The duplicate check is per (owner, uploader, checksum): two people may each
    upload the same photo to a shared wiki, but one person can't upload it to
    the same place twice. The same person uploading the same bytes to a
    *different* pin reuses the stored file (``QuotaExemption.DEDUPLICATED``)
    instead of charging quota twice.

    The quota check and the row insert are taken under ``per_profile_upload_lock``
    so two concurrent uploads can't both read the same pre-upload usage figure
    and jointly exceed the profile's quota.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) to attach the photo to.
        profile: The uploading profile.
        image_file: The uploaded file.
        caption: Optional caption; blank becomes None.

    Returns:
        The created :class:`Image`, or an :class:`UploadRejection` explaining
        why it was refused. Callers are expected to branch on the type rather
        than assume success.


    **The caller must enqueue ``tasks.process_image_upload`` for a newly stored
    row.** Deduplicated copies skip that task: they point at a file that is
    already (or will be) processed on the original row. Enforced by a test
    rather than by this function because the task dispatch belongs to the
    request cycle; see ``test_photo_upload_dispatches_processing.py``.
    """
    if (upload_error := image_upload_error(image_file, MediaKind.PHOTO)) is not None:
        message, status = upload_error
        return UploadRejection(message, status)

    # Of the *uploaded* bytes, before the strip below rewrites them: the
    # checksum identifies what the user sent, and is what dedup matches on.
    checksum = compute_checksum(image_file)
    if existing_photo_for_upload(owner, profile, checksum=checksum) is not None:
        _scope, noun = _duplicate_scope(owner)
        return UploadRejection(f"You already uploaded this photo to this {noun}.", 409)

    elsewhere = existing_photo_for_profile(profile, checksum)
    if elsewhere is not None:
        caption_error = column_length_error(Image, "caption", caption, "Caption")
        if caption_error:
            return UploadRejection(caption_error, 400)
        return attach_deduped_copy(elsewhere, owner, profile, caption)

    with per_profile_upload_lock(profile):
        if (quota_error := quota_error_for_upload(profile, image_file.size)) is not None:
            return UploadRejection(quota_error, 413)

        caption_error = column_length_error(Image, "caption", caption, "Caption")
        if caption_error:
            return UploadRejection(caption_error, 400)
        # Read the metadata and remove it from the bytes in one step, so an
        # unstripped original is never written to the media tree - see
        # prepare_photo_upload.
        prepared = prepare_photo_upload(image_file, profile)
        return Image.objects.create(
            image=prepared.file,
            profile=profile,
            caption=caption.strip() or prepared.metadata_caption or None,
            checksum=checksum,
            file_size=prepared.size,
            **_owner_fields(owner),
            **prepared.metadata,
        )
