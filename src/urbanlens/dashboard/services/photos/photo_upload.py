"""Shared photo/video/document upload pipeline.

Extracted from ``controllers.photos.PhotoUploadView.post`` so the Memories
page's drag-and-drop uploader and the external API's ``POST photos/`` run
byte-for-byte the same admission checks: media-type sniffing, the per-account
video/document feature gates, the malware/size/dimension checks in
``services.media.images.image_upload_error``, per-profile duplicate rejection, and
the storage quota. A second implementation of any of those on the API side
would be a second place for them to be wrong - and the quota check in
particular is only sound because every writer takes the same lock.

The web controller and the API differ only in how a failure is rendered
(``JsonResponse`` vs. DRF ``Response``), which is why this raises
:class:`PhotoUploadError` carrying an HTTP status rather than returning a
response of either kind.
"""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.core.text_limits import column_length_error

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.visits.model import PinVisit
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class PhotoUploadError(Exception):
    """An upload the caller may not perform, carrying the status to answer with.

    Attributes:
        message: User-facing explanation, safe to return to an untrusted caller.
        status: The HTTP status the calling view should respond with (400 for
            an unusable file, 403 for a feature the account lacks, 409 for a
            duplicate, 413 for a quota overrun).
    """

    def __init__(self, message: str, status: int) -> None:
        """Store the user-facing message and the HTTP status it maps to.

        Args:
            message: User-facing explanation of the refusal.
            status: HTTP status code the caller should respond with.
        """
        super().__init__(message)
        self.message = message
        self.status = status


def _resolve_media_type(file_obj: UploadedFile, profile: Profile) -> MediaKind:
    """Classify an upload as photo/video/document and enforce the per-account gates.

    Args:
        file_obj: The uploaded file, whose ``content_type`` and name extension
            are both consulted (documents are commonly served as
            ``application/octet-stream``, so the extension is the fallback).
        profile: The uploading profile, whose user's feature grants decide
            whether video and document uploads are permitted at all.

    Returns:
        The resolved :class:`MediaKind`.

    Raises:
        PhotoUploadError: The file is not a supported type (400), or is a
            video/document the account isn't entitled to upload (403).
    """
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
    from urbanlens.dashboard.services.media.documents import DOCUMENT_EXTENSIONS

    content_type = file_obj.content_type or ""
    extension = posixpath.splitext(file_obj.name or "")[1].lower()

    if content_type.startswith("video/"):
        if not user_has_feature(profile.user, SiteFeature.VIDEO_UPLOADS):
            raise PhotoUploadError("Video uploads are not enabled for your account.", 403)
        return MediaKind.VIDEO
    if content_type.startswith("image/"):
        return MediaKind.PHOTO
    if content_type == "application/pdf" or extension in DOCUMENT_EXTENSIONS:
        if not user_has_feature(profile.user, SiteFeature.DOCUMENT_UPLOADS):
            raise PhotoUploadError("Document uploads are not enabled for your account.", 403)
        return MediaKind.DOCUMENT
    raise PhotoUploadError("That file is not an image, video, or supported document type.", 400)


def upload_photo(
    profile: Profile,
    file_obj: UploadedFile,
    *,
    caption: str | None = None,
    pin: Pin | None = None,
    visit: PinVisit | None = None,
    wiki: Wiki | None = None,
) -> Image:
    """Admit one uploaded file as an ``Image`` row and queue its metadata ingestion.

    The caller is responsible for having established that *profile* owns
    (or may write to) any ``pin``/``visit``/``wiki`` passed in - this function
    attaches them as given and does not re-check ownership.

    Args:
        profile: The uploading profile; the file counts against its quota.
        file_obj: The uploaded file.
        caption: Optional caption to store on the new row.
        pin: Optional pin to file the upload into directly.
        visit: Optional visit to attach the upload to.
        wiki: Optional wiki gallery to publish the upload into.

    Returns:
        The created :class:`Image`. EXIF-derived fields (coordinates,
        ``taken_at``, ``author``) are populated asynchronously by
        ``tasks.process_image_upload`` and are typically still unset on the
        returned instance.

    Raises:
        PhotoUploadError: The upload was refused; see the exception's
            ``status`` for how to answer the caller.
    """
    from urbanlens.dashboard.services.media.images import compute_checksum, image_upload_error, prepare_photo_upload
    from urbanlens.dashboard.services.media.storage import per_profile_upload_lock, quota_error_for_upload

    media_type = _resolve_media_type(file_obj, profile)

    upload_error = image_upload_error(file_obj, media_type)
    if upload_error:
        message, status = upload_error
        raise PhotoUploadError(message, status)

    caption_error = column_length_error(Image, "caption", caption, "Caption")
    if caption_error:
        raise PhotoUploadError(caption_error, 400)

    # Of the *uploaded* bytes, before the strip below rewrites them: the
    # checksum identifies what the user sent, and is what dedup matches on.
    checksum = compute_checksum(file_obj)
    if Image.objects.filter(profile=profile, checksum=checksum).exists():
        raise PhotoUploadError("You already uploaded this file.", 409)

    # Read the metadata and remove it from the bytes in one step, so an
    # unstripped original is never written to the media tree - see
    # prepare_photo_upload. Videos and documents have their own pipelines.
    prepared = prepare_photo_upload(file_obj, profile) if media_type == MediaKind.PHOTO else None

    # The quota read and the row create must be one critical section: without
    # it, N concurrent uploads can each pass the check before any commits.
    with per_profile_upload_lock(profile):
        quota_error = quota_error_for_upload(profile, file_obj.size)
        if quota_error:
            raise PhotoUploadError(quota_error, 413)

        image = Image.objects.create(
            image=prepared.file if prepared else file_obj,
            profile=profile,
            checksum=checksum,
            file_size=prepared.size if prepared else file_obj.size,
            media_type=media_type,
            caption=(caption or (prepared.metadata_caption if prepared else None) or None),
            pin=pin,
            visit=visit,
            wiki=wiki,
            **(prepared.metadata if prepared else {}),
        )

    # Deliberately outside the lock: enqueuing is a network call to the broker,
    # and holding a per-profile upload lock across it would serialize an
    # unrelated cost onto the next upload.
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import process_image_upload

    safely_enqueue_task(process_image_upload, image.pk)
    return image
