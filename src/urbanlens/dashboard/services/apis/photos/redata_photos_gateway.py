"""Gateway for REData's photo-relevance-scoring endpoints (``/photos/...``).

REData scores how likely a photo is to really be a photo of the place it's
attached to (distance, parcel containment, date plausibility, contributor
reputation, image quality), refining its model over time from UrbanLens's own
relevance votes. Every score comes back as ``confidence`` (a calibrated
probability, always in ``[0.02, 0.98]``) plus which ``scorer`` produced it
(``"heuristic"`` or ``"model"``).

Same REData account/deployment already used for property records
(``services.apis.property_records.redata_gateway.RedataGateway``) and places
resolution (``locations.google.redata_places_gateway.RedataPlacesGateway``) -
same ``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` settings, same bearer-token
convention, hitting different endpoints. The API key used must carry REData's
``photos:read``/``photos:write`` scopes.

Do not call this directly - go through ``services.photos.redata_relevance``,
which decides when a photo/vote is worth submitting and caches the returned
confidence back onto the ``Image`` row.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30

#: REData's own per-request caps - callers batch larger lists themselves.
MAX_PHOTOS_PER_SUBMIT = 200
MAX_VOTES_PER_SUBMIT = 1000
MAX_PHOTO_IDS_PER_CONFIDENCE_LOOKUP = 1000


@dataclass(slots=True, kw_only=True)
class RedataPhotosGateway(Gateway):
    """REST client for REData's ``/photos/...`` endpoints."""

    service_key: ClassVar[str] = "redata_photos"
    paid_service: ClassVar[bool] = False

    base_url: str | None = settings.redata_api_url
    api_key: str | None = settings.redata_api_key

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST one REData ``/photos/...`` endpoint and return its decoded JSON body.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            body: JSON request body.

        Returns:
            The decoded JSON response body.

        Raises:
            GatewayRequestError: The request itself could not be made (network
                failure), REData returned a non-2xx status, or the response
                body wasn't parseable JSON.
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(f"{base_url.rstrip('/')}/{path.lstrip('/')}", json=body, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code in (200, 201):
            try:
                return dict(response.json())
            except ValueError as exc:
                raise GatewayRequestError("REData returned an unparseable response.") from exc

        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise GatewayRequestError(f"REData request to {path} failed with status {response.status_code}.")

    def submit_photos(self, photos: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit (upsert) photo observations for scoring.

        Args:
            photos: Up to :data:`MAX_PHOTOS_PER_SUBMIT` submission dicts, each
                keyed by ``photo_id`` (UrbanLens's own id - see
                ``services.photos.redata_relevance``). A field REData doesn't
                recognize a value for is simply omitted rather than sent as
                ``None`` - see that module's docstring for why.

        Returns:
            ``{count, results, unknown, created, updated, image_warnings,
            pending}`` - ``results`` maps ``photo_id`` to its confidence
            record (``confidence``, ``scorer``, ``model_version``,
            ``scored_at``, ``upvotes``, ``downvotes``); ``pending`` lists ids
            still queued for scoring (image analysis in progress).

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        if not photos:
            return {"count": 0, "results": {}, "unknown": [], "created": 0, "updated": 0, "image_warnings": {}, "pending": []}
        return self._post_json("/api/v1/photos/", {"photos": photos})

    def submit_votes(self, votes: list[dict[str, Any]]) -> dict[str, Any]:
        """Record relevance votes - the model's training label, never a scoring input.

        Args:
            votes: Up to :data:`MAX_VOTES_PER_SUBMIT` vote dicts (``photo_id``,
                ``is_relevant``, and optionally ``voter_id``/``voted_at``).

        Returns:
            ``{recorded, unknown_photo_ids, updated_photos}`` - a vote for a
            photo REData was never told about is reported in
            ``unknown_photo_ids``, not auto-created.

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        if not votes:
            return {"recorded": 0, "unknown_photo_ids": [], "updated_photos": 0}
        return self._post_json("/api/v1/photos/votes/", {"votes": votes})

    def get_confidence_batch(self, photo_ids: list[str]) -> dict[str, Any]:
        """Look up cached confidence scores for many photos at once.

        Args:
            photo_ids: Up to :data:`MAX_PHOTO_IDS_PER_CONFIDENCE_LOOKUP` ids.

        Returns:
            ``{count, results, unknown}`` - ``results`` maps ``photo_id`` to
            its confidence record; ``unknown`` lists ids REData has never
            been told about.

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        if not photo_ids:
            return {"count": 0, "results": {}, "unknown": []}
        return self._post_json("/api/v1/photos/confidence/", {"photo_ids": photo_ids})
