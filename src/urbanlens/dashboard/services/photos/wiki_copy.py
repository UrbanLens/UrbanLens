"""Copying a wiki photo onto the copier's own pin, with durable authorship provenance.

A wiki photo belongs to whoever uploaded it. Copying one onto your own pin must not let the copy
silently pass as your own work, and must not let the original later disappearing take your copy's
attribution with it - see :class:`~urbanlens.dashboard.models.images.model.Image`'s
``copied_from*`` fields for exactly what is captured at copy time and why each is denormalized
rather than resolved live.

Mirrors :func:`~urbanlens.dashboard.services.sharing.pin_sharing.create_pin_from_share`'s
copy-without-duplicating-bytes shape and its "what travels with a photo" rules (dates, lat/lng,
direction, and checksum travel; caption and exif_data do not; an unattributed photo gets the
original uploader's name, one that already credits somebody keeps that credit) - see that
function's docstring for the full reasoning, which applies here unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import Image, QuotaExemption

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile


def copy_wiki_photo_to_pin(image: Image, target_pin: Pin, profile: Profile) -> tuple[Image, bool]:
    """Copy a wiki photo onto ``target_pin``, crediting its original uploader.

    Reuses the wiki photo's own stored file rather than duplicating bytes, and does not re-run
    upload processing - the new row points at an already-processed file, exactly like
    ``create_pin_from_share``'s copies. Costs ``profile`` no storage of their own
    (``QuotaExemption.WIKI_COPY``).

    Idempotent: calling this again for the same (``image``, ``target_pin``) pair returns the
    existing copy rather than creating a second one.

    Args:
        image: The wiki photo being copied. Callers must have already verified ``profile`` can
            see it on this wiki - this function does not check visibility.
        target_pin: The pin the copy is filed under.
        profile: The profile making the copy (the new row's owner).

    Returns:
        ``(copy, created)`` - the copy row, and whether it was newly created (``False`` when
        ``target_pin`` already had a copy of this exact photo).
    """
    existing = Image.objects.filter(pin=target_pin, copied_from=image).first()
    if existing is not None:
        return existing, False

    if image.location is not None:
        label = image.location.display_name or ""
    elif image.wiki is not None:
        label = image.wiki.name or ""
    else:
        label = ""

    if image.author:
        author = image.author
    elif image.profile is not None:
        author = f"Uploaded by {image.profile.username}"
    else:
        author = ""

    copy = Image.objects.create(
        image=image.image.name,
        pin=target_pin,
        location=target_pin.location,
        profile=profile,
        quota_exempt_reason=QuotaExemption.WIKI_COPY,
        copied_from=image,
        copied_from_profile=image.profile,
        copied_from_location=image.location,
        copied_from_label=label,
        media_type=image.media_type,
        source=image.source,
        author=author,
        source_url=image.source_url,
        copyright=image.copyright,
        latitude=image.latitude,
        longitude=image.longitude,
        direction=image.direction,
        checksum=image.checksum,
        taken_at=image.taken_at,
        filename_taken_at=image.filename_taken_at,
        file_size=image.file_size,
    )
    return copy, True
