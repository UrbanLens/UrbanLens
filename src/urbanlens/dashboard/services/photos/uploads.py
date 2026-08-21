"""Creating a photo from a browser upload, for a Pin or a Wiki.

Three surfaces turn an uploaded file into an :class:`Image`: the pin gallery,
the wiki gallery, and an album's own upload button. They must agree on every
gate an upload passes - file-type/malware validation, the per-uploader
duplicate check, and the storage quota - so those gates live here rather than
being restated per view. A surface that skips one would let a file in that the
others reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.text_limits import column_length_error
from urbanlens.dashboard.services.media.images import compute_checksum, image_upload_error
from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class UploadRejection:
    """A refused upload, carrying the message and status the caller should return.

    Attributes:
        message: User-facing explanation, safe to show in a toast.
        status: HTTP status describing the refusal (400/409/413/415).
    """

    message: str
    status: int


def _owner_fields(owner: Pin | Wiki) -> dict:
    """Return the Image ownership FKs for *owner*.

    A pin photo is also attached to the location's wiki when one exists, which
    is what makes "send to wiki" a no-op for photos uploaded after the wiki was
    created; a wiki photo has no pin.

    Args:
        owner: The Pin or Wiki the photo is being uploaded to.

    Returns:
        Kwargs for ``Image.objects.create``.
    """
    if isinstance(owner, Pin):
        location: Location | None = owner.location
        return {"pin": owner, "wiki": Wiki.objects.get_for_location(location), "location": location}
    return {"wiki": owner, "location": owner.location}


def _duplicate_scope(owner: Pin | Wiki) -> tuple[dict, str]:
    """Return the filter isolating *owner*'s photos, and the noun for the error.

    Args:
        owner: The Pin or Wiki being uploaded to.

    Returns:
        Tuple of (queryset filter kwargs, the word to use in "already uploaded
        this photo to this <noun>").
    """
    return ({"pin": owner}, "pin") if isinstance(owner, Pin) else ({"wiki": owner}, "wiki")


def upload_photo_for_owner(owner: Pin | Wiki, profile: Profile, image_file: UploadedFile, caption: str = "") -> Image | UploadRejection:
    """Validate and store one uploaded photo against *owner*.

    The duplicate check is per (owner, uploader, checksum): two people may each
    upload the same photo to a shared wiki, but one person can't upload it to
    the same place twice.

    The quota check and the row insert are taken under ``per_profile_upload_lock``
    so two concurrent uploads can't both read the same pre-upload usage figure
    and jointly exceed the profile's quota.

    Args:
        owner: The Pin or Wiki to attach the photo to.
        profile: The uploading profile.
        image_file: The uploaded file.
        caption: Optional caption; blank becomes None.

    Returns:
        The created :class:`Image`, or an :class:`UploadRejection` explaining
        why it was refused. Callers are expected to branch on the type rather
        than assume success.


    **The caller must enqueue ``tasks.process_image_upload`` for the returned
    row.** This function stores the file as uploaded - EXIF (including GPS)
    stripping, downscaling and format conversion all happen in that task, so
    a caller that skips it leaves a user's camera GPS in a stored, servable
    file. Enforced by a test rather than by this function because the task
    dispatch belongs to the request cycle (it is the controller that knows
    whether to respond before or after enqueueing); see
    ``test_photo_upload_dispatches_processing.py``.
    """
    if (upload_error := image_upload_error(image_file, MediaKind.PHOTO)) is not None:
        message, status = upload_error
        return UploadRejection(message, status)

    checksum = compute_checksum(image_file)
    scope, noun = _duplicate_scope(owner)
    if Image.objects.filter(profile=profile, checksum=checksum, **scope).exists():
        return UploadRejection(f"You already uploaded this photo to this {noun}.", 409)

    with per_profile_upload_lock(profile):
        if (quota_error := quota_error_for_upload(profile, image_file.size)) is not None:
            return UploadRejection(quota_error, 413)

        caption_error = column_length_error(Image, "caption", caption, "Caption")
        if caption_error:
            return UploadRejection(caption_error, 400)
        return Image.objects.create(
            image=image_file,
            profile=profile,
            caption=caption.strip() or None,
            checksum=checksum,
            file_size=image_file.size,
            **_owner_fields(owner),
        )
