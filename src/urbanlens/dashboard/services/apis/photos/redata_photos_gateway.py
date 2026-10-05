"""Gateway for REData's photo-relevance-scoring endpoints (``/photos/...``, and a parcel's photos)."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar
from urllib.parse import quote

from urbanlens.dashboard.services.apis.redata_json_gateway import RedataJsonGateway
from urbanlens.dashboard.services.core.environment import skip_upstream_contribution
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30

#: REData's own per-request caps - callers batch larger lists themselves.
MAX_PHOTOS_PER_SUBMIT = 200
MAX_VOTES_PER_SUBMIT = 1000
MAX_PHOTO_IDS_PER_CONFIDENCE_LOOKUP = 1000


def _empty_submit_result() -> dict[str, Any]:
    """A "nothing was submitted" result.
    Shared by the empty-input and the skipped-off-production paths so the two are indistinguishable to callers."""
    return {"count": 0, "results": {}, "unknown": [], "created": 0, "updated": 0, "image_warnings": {}, "pending": []}


def _empty_vote_result(votes: list[dict[str, Any]]) -> dict[str, Any]:
    """A "no votes recorded" result reporting every supplied vote's photo as unknown."""
    return {"recorded": 0, "unknown_photo_ids": [str(vote["photo_id"]) for vote in votes if vote.get("photo_id") is not None], "updated_photos": 0}


def _photo_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``results`` of a photo collection body, keeping only rows that name a photo."""
    results = body.get("results")
    return [row for row in results if isinstance(row, dict) and row.get("photo_id")] if isinstance(results, list) else []


#: Read inline in the site-admin page render, so it is bounded well below
#: the default: a diagnostics page that hangs for half a minute because
#: REData is unresponsive is reporting the outage by being one.
_MODEL_READ_TIMEOUT = 5


@dataclass(slots=True, kw_only=True)
class RedataPhotosGateway(RedataJsonGateway):
    """REST client for REData's ``/photos/...`` endpoints."""

    service_key: ClassVar[str] = "redata_photos"
    paid_service: ClassVar[bool] = False

    # default_factory so settings changes apply per instance; a bare default freezes at import.
    base_url: str | None = field(default_factory=lambda: settings.redata_api_url)
    api_key: str | None = field(default_factory=lambda: settings.redata_api_key)

    def get_model(self) -> dict[str, Any]:
        """Return what is currently scoring photo relevance, and how well.
        Brier score is the headline metric on purpose: it is a proper scoring rule, so unlike AUC it penalises a model that ranks well while being systematically overconfident.

        Returns:
            The decoded ``GET /photos/model/`` body.

        Raises:
            GatewayRequestError: The request to REData failed."""
        return self._get_json("/api/v1/photos/model/", timeout=_MODEL_READ_TIMEOUT)

    def submit_photos(self, photos: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit (upsert) photo observations for scoring.

        Args:
            photos: Up to :data:`MAX_PHOTOS_PER_SUBMIT` submission dicts, each
                keyed by ``photo_id`` (UrbanLens's own id - see
                ``services.photos.redata_relevance``). A field REData doesn't
                recognize a value for is simply omitted rather than sent as
                ``None`` - see that module's docstring for why.

        Returns:
            ``{count, results, unknown, created, updated, image_warnings, pending}`` - ``results`` maps ``photo_id`` to its confidence record (``confidence``, ``scorer``, ``model_version``, ``scored_at``, ``upvotes``, ``downvotes``); ``pending`` lists ids...

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status.
        """
        if not photos:
            return _empty_submit_result()
        if skip_upstream_contribution("REData photo observations (POST /photos/)", detail=f"{len(photos)} photo(s)"):
            return _empty_submit_result()
        return self._post_json("/api/v1/photos/", {"photos": photos})

    def submit_votes(self, votes: list[dict[str, Any]]) -> dict[str, Any]:
        """Record relevance votes - the model's training label, never a scoring input.

        Args:
            votes: Up to :data:`MAX_VOTES_PER_SUBMIT` vote dicts (``photo_id``,
                ``is_relevant``, and optionally ``voter_id``/``voted_at``).

        Returns:
            ``{recorded, unknown_photo_ids, updated_photos}`` - a vote for a photo REData was never told about is reported in ``unknown_photo_ids``, not auto-created.

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status.
        """
        if not votes:
            return _empty_vote_result([])
        if skip_upstream_contribution("REData photo relevance votes (POST /photos/votes/)", detail=f"{len(votes)} vote(s)"):
            return _empty_vote_result(votes)
        return self._post_json("/api/v1/photos/votes/", {"votes": votes})

    def lookup_near(self, latitude: float, longitude: float, *, radius_meters: float | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Every photo this site submitted for a place near a coordinate, matched on the place rather than the camera.

        A local read on REData's side - it never answers 503 - that scores any photo with no current score first.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: How far from the point a photo's place may be; REData defaults to 150 m and caps at 5 km.
            limit: Most photos to return; REData defaults to 100.

        Returns:
            ``PhotoSerializer`` rows (``photo_id`` - this site's own image uuid - ``confidence``, ``scorer``,
            ``location_latitude``/``location_longitude``, ``parcel_uuid``, ``taken_at``, ...).

        Raises:
            GatewayRequestError: The request failed, or REData answered with a non-2xx status.
        """
        params: dict[str, Any] = {"lat": latitude, "lng": longitude}
        if radius_meters is not None:
            params["radius_meters"] = radius_meters
        if limit is not None:
            params["limit"] = limit
        return _photo_rows(self._get_json("/api/v1/photos/lookup/", params))

    def photos_for_parcel(self, parcel_uuid: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Every photo whose place REData resolved to a parcel, best-scoring first.

        Args:
            parcel_uuid: The parcel's REData uuid.
            limit: Most photos to return; REData defaults to 100.

        Returns:
            ``PhotoSerializer`` rows, as :meth:`lookup_near`.

        Raises:
            GatewayRequestError: The request failed, or REData answered with a non-2xx status - including a ``404``
                for a parcel it does not hold.
        """
        params = {"limit": limit} if limit is not None else None
        return _photo_rows(self._get_json(f"/api/v1/parcels/{quote(parcel_uuid, safe='')}/photos/", params))

    def get_confidence_batch(self, photo_ids: list[str]) -> dict[str, Any]:
        """Look up cached confidence scores for many photos at once.

        Args:
            photo_ids: Up to :data:`MAX_PHOTO_IDS_PER_CONFIDENCE_LOOKUP` ids.

        Returns:
            ``{count, results, unknown}`` - ``results`` maps ``photo_id`` to its confidence record; ``unknown`` lists ids REData has never been told about.

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status.
        """
        if not photo_ids:
            return {"count": 0, "results": {}, "unknown": []}
        return self._post_json("/api/v1/photos/confidence/", {"photo_ids": photo_ids})
