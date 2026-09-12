"""Gateway for REData's photo-relevance-scoring endpoints (``/photos/...``)."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

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

        Returns:
            ``{count, results, unknown, created, updated, image_warnings, pending}`` - ``results`` maps ``photo_id`` to its confidence record (``confidence``, ``scorer``, ``model_version``, ``scored_at``, ``upvotes``, ``downvotes``); ``pending`` lists ids...

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status."""
        if not photos:
            return _empty_submit_result()
        if skip_upstream_contribution("REData photo observations (POST /photos/)", detail=f"{len(photos)} photo(s)"):
            return _empty_submit_result()
        return self._post_json("/api/v1/photos/", {"photos": photos})

    def submit_votes(self, votes: list[dict[str, Any]]) -> dict[str, Any]:
        """Record relevance votes - the model's training label, never a scoring input.

        Returns:
            ``{recorded, unknown_photo_ids, updated_photos}`` - a vote for a photo REData was never told about is reported in ``unknown_photo_ids``, not auto-created.

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status."""
        if not votes:
            return _empty_vote_result([])
        if skip_upstream_contribution("REData photo relevance votes (POST /photos/votes/)", detail=f"{len(votes)} vote(s)"):
            return _empty_vote_result(votes)
        return self._post_json("/api/v1/photos/votes/", {"votes": votes})

    def get_confidence_batch(self, photo_ids: list[str]) -> dict[str, Any]:
        """Look up cached confidence scores for many photos at once.

        Returns:
            ``{count, results, unknown}`` - ``results`` maps ``photo_id`` to its confidence record; ``unknown`` lists ids REData has never been told about.

        Raises:
            GatewayRequestError: The request failed outright, or REData reported a non-2xx status."""
        if not photo_ids:
            return {"count": 0, "results": {}, "unknown": []}
        return self._post_json("/api/v1/photos/confidence/", {"photo_ids": photo_ids})
