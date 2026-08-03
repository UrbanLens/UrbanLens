"""Wires UrbanLens's photos up to REData's photo-relevance-scoring service.

Two kinds of data flow out to REData (``services.apis.photos.redata_photos_gateway``):

- **Observations** (``POST /photos/``), whenever a new photo is uploaded or
  discovered from an external source - see :func:`queue_photo_submission`.
  REData scores it immediately and the response's confidence is cached back
  onto the ``Image`` row (:data:`Image.redata_confidence` and friends) so
  pin-detail/wiki galleries can order by it without a live call on every
  render.
- **Votes** (``POST /photos/votes/``), whenever a user marks a photo
  relevant/not relevant - see :func:`queue_relevance_vote`. Votes are the
  model's training label, never a scoring input, so this never touches the
  cached confidence itself.

Both are best-effort: a missing/unreachable REData deployment (the common
case for most installs - see ``_redata_configured``) is a silent no-op, not
an error, exactly like every other REData integration in this codebase (see
``services.apis.locations.places_resolution._redata_configured``).

Submission omits any field UrbanLens has no value for, rather than sending an
explicit ``None`` - REData's own docs describe a missing signal as "treated
as missing rather than imputed", i.e. a sparse submission still gets a real
(just less confident) answer, so there's no reason to send a null placeholder
for a photo with no capture GPS, no known photographer, etc.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from django.utils import timezone

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def _redata_configured() -> bool:
    """Whether REData is configured, mirroring ``places_resolution._redata_configured``."""
    from urbanlens.UrbanLens.settings.app import settings

    return bool(settings.redata_api_url and settings.redata_api_key)


def _source_host(image: Image) -> str | None:
    """Best-effort site host this photo came from, for REData's ``source`` field.

    Only meaningful for a photo materialized from an external provider
    (``source_url`` set) - a plain personal upload has no "site" to report,
    so this returns None for one rather than something like ``"upload"``.
    """
    if not image.source_url:
        return None
    host = urlparse(image.source_url).hostname
    return host or None


def _years_from_abandoned(image: Image) -> float | None:
    """Years between this photo's capture and the location's wiki-recorded abandonment date.

    Negative when the photo predates abandonment, per REData's own field
    convention. None when either side of the comparison is unknown.
    """
    if image.taken_at is None:
        return None

    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = image.wiki
    if wiki is None and image.location_id is not None:
        wiki = Wiki.objects.filter(location_id=image.location_id).first()
    if wiki is None or wiki.date_abandoned is None:
        return None

    delta_days = (image.taken_at.date() - wiki.date_abandoned).days
    return round(delta_days / 365.25, 2)


def _submission_payload(image: Image) -> dict[str, Any] | None:
    """Build one ``POST /photos/`` submission dict for ``image``, or None if it can't be scored.

    Args:
        image: A photo with ``location`` set - REData scores relevance
            against a place, so a photo with no resolved location has
            nothing to submit yet (it may be submitted later, once
            ``tasks.process_image_upload`` resolves one).

    Returns:
        The submission dict, or None when ``image`` has no usable location
        coordinates at all (its own, and its Location's, are both unset).
    """
    from urbanlens.dashboard.models.images.model import Image

    latitude = image.effective_latitude
    longitude = image.effective_longitude
    location = image.location
    if location is not None and location.latitude is not None and location.longitude is not None:
        latitude, longitude = location.latitude, location.longitude
    if latitude is None or longitude is None:
        return None

    payload: dict[str, Any] = {
        "photo_id": str(image.uuid),
        "location_latitude": float(latitude),
        "location_longitude": float(longitude),
    }
    if image.latitude is not None and image.longitude is not None:
        payload["photo_latitude"] = float(image.latitude)
        payload["photo_longitude"] = float(image.longitude)
    if image.taken_at is not None:
        payload["taken_at"] = image.taken_at.isoformat()
    if image.profile_id is not None:
        payload["uploader_id"] = str(image.profile_id)
        payload["uploader_photo_count"] = Image.objects.filter(profile_id=image.profile_id).count()
    if image.author:
        payload["photographer"] = image.author
    source_host = _source_host(image)
    if source_host:
        payload["source"] = source_host
    years_from_abandoned = _years_from_abandoned(image)
    if years_from_abandoned is not None:
        payload["years_from_abandoned"] = years_from_abandoned
    if image.source:
        payload["extra_attributes"] = {"urbanlens_image_source": image.source}
    return payload


def submit_photos(images: list[Image]) -> None:
    """Submit observations for ``images`` to REData and cache the confidence each gets back.

    Called from the ``submit_redata_photos`` Celery task - never call this
    synchronously from a request/view. Best-effort: a REData request failure
    is logged and swallowed rather than raised, matching every other
    best-effort REData call site in this codebase (e.g.
    ``tasks.import_immich_photos``) - a photo simply stays unscored (falls
    back to upload-order ranking) until a later submission succeeds.

    Args:
        images: Photos to submit - each is skipped (not sent) when it has no
            usable location coordinates yet.
    """
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    if not _redata_configured():
        return

    by_photo_id: dict[str, Image] = {}
    submissions = []
    for image in images:
        payload = _submission_payload(image)
        if payload is None:
            continue
        submissions.append(payload)
        by_photo_id[payload["photo_id"]] = image
    if not submissions:
        return

    try:
        response = RedataPhotosGateway().submit_photos(submissions)
    except GatewayRequestError as exc:
        logger.warning("REData photo submission failed for %d photo(s): %s", len(submissions), exc)
        return

    from django.utils.dateparse import parse_datetime

    results = response.get("results") or {}
    now = timezone.now()
    for photo_id, record in results.items():
        scored_image = by_photo_id.get(photo_id)
        if scored_image is None or not isinstance(record, dict):
            continue
        confidence = record.get("confidence")
        if confidence is None:
            continue
        scored_at_raw = record.get("scored_at")
        scored_at = (parse_datetime(scored_at_raw) if isinstance(scored_at_raw, str) else None) or now
        Image.objects.filter(pk=scored_image.pk).update(
            redata_confidence=confidence,
            redata_scorer=record.get("scorer") or None,
            redata_model_version=record.get("model_version"),
            redata_scored_at=scored_at,
        )


def queue_photo_submission(image: Image) -> None:
    """Queue ``image`` for REData submission, if REData is configured and it has a location.

    Safe to call unconditionally from any photo-creation call site (upload
    processing, external-source enrichment, Media-gallery materialization) -
    silently does nothing when REData isn't configured or the photo has no
    location yet, so callers never need their own guard.

    Args:
        image: The newly created (or newly located) photo.
    """
    if not _redata_configured() or image.location_id is None:
        return

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import submit_redata_photos

    safely_enqueue_task(submit_redata_photos, [image.pk])


def queue_relevance_vote(image: Image, profile: Profile, *, is_relevant: bool) -> None:
    """Queue one relevance vote on ``image`` for submission to REData.

    Safe to call unconditionally from any vote call site - silently does
    nothing when REData isn't configured. REData has no "retract a vote"
    endpoint, so this is only meaningful for an explicit relevant/not-relevant
    vote, not for clearing one back to neutral (callers should simply not
    call this for a clear).

    Args:
        image: The photo being voted on. If REData was never told about this
            photo (no location at submission time, or the submission hasn't
            landed yet), the vote comes back in REData's own
            ``unknown_photo_ids`` and is otherwise a no-op on REData's side -
            see ``services.apis.photos.redata_photos_gateway.RedataPhotosGateway.submit_votes``.
        profile: The voting profile.
        is_relevant: True for a relevant vote, False for not-relevant.
    """
    if not _redata_configured():
        return

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import submit_redata_photo_vote

    safely_enqueue_task(submit_redata_photo_vote, image.pk, profile.pk, is_relevant)
