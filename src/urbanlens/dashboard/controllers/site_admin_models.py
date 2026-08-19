"""Site-admin view: what REData's two models are, and how well they score.

Two questions this deployment could not previously answer about itself:

- Is a *trained model* answering, or the hand-weighted heuristic? Both are
  legitimate - REData promotes a model only when it beats the incumbent and
  the heuristic on the same held-out split - but they are calibrated
  differently, and auto-tagging applies suggestions above a fixed confidence
  floor without distinguishing them. "Why is auto-tagging suggesting nonsense?"
  is unanswerable without knowing which ranker produced the number.
- How good is the thing that is answering? REData reports holdout metrics
  against the baselines the promotion decision was made on, which is the
  comparison worth seeing rather than an absolute score in isolation.

**Nothing here is about a person.** Both endpoints report on the models
themselves. REData's per-contributor endpoint (``GET /photos/reputation/``)
is deliberately not consumed anywhere in this codebase: a reputation score for
an individual is not something this application has a use for. The scrubbing
below enforces that at the boundary rather than trusting the upstream shape to
stay aggregate.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views import View

logger = logging.getLogger(__name__)

#: Keys refused from any upstream model payload before it reaches a template.
#: None of these appear in the documented response - the point is that if a
#: future REData grows a per-person field, it stops here rather than being
#: rendered because nobody re-read the contract.
_PERSONAL_KEYS = frozenset(
    {
        "contributor",
        "contributors",
        "email",
        "key",
        "photographer",
        "reputation",
        "reputations",
        "source_id",
        "uploader",
        "user",
        "user_id",
        "users",
    },
)


def scrub_personal_keys(value: Any) -> Any:
    """Recursively drop anything that could identify a person.

    Applied to model metadata that should be aggregate by construction, so in
    normal operation this removes nothing. It exists because "aggregate" is a
    property of the current upstream shape rather than something this codebase
    can enforce upstream, and a diagnostics page is a poor place to discover
    that the shape changed.

    Args:
        value: A decoded JSON fragment.

    Returns:
        The same structure with personal keys removed at every depth.
    """
    if isinstance(value, dict):
        return {key: scrub_personal_keys(item) for key, item in value.items() if key.lower() not in _PERSONAL_KEYS}
    if isinstance(value, list):
        return [scrub_personal_keys(item) for item in value]
    return value


def _model_summary(fetch, label: str) -> dict[str, Any]:
    """Fetch one model's metadata, or describe why it is unavailable.

    Args:
        fetch: Zero-argument callable returning the raw endpoint body.
        label: Human name for the model, used in the error line.

    Returns:
        A dict carrying ``available``, plus either the scrubbed payload or a
        ``message`` saying what went wrong. An unreachable REData is reported
        rather than raised: a diagnostics page that 500s when the thing it
        diagnoses is down is the least useful moment to lose it.
    """
    from urbanlens.dashboard.services.core.gateway import GatewayRequestError

    try:
        payload = scrub_personal_keys(fetch() or {})
    except GatewayRequestError as exc:
        logger.info("REData %s model metadata unavailable: %s", label, exc)
        return {"available": False, "message": str(exc)}

    active = payload.get("active")
    return {
        "available": True,
        # `active: null` is normal, not an error - it means no model has been
        # promoted yet and the heuristic is answering.
        "active": active,
        "has_model": active is not None,
        "metrics": payload.get("metrics") or {},
        "ranking_metrics": payload.get("ranking_metrics") or {},
        "baseline_metrics": payload.get("baseline_metrics") or {},
        "features": payload.get("features") or payload.get("feature_registry") or [],
        "schema_fingerprint": payload.get("schema_fingerprint") or payload.get("feature_schema_fingerprint") or "",
    }


class SiteAdminModelsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Read-only diagnostics for REData's label-suggestion and photo-relevance models.

    GET /site-admin/models/
    """

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; 403 for authenticated users without permission."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render both models' current state.

        Args:
            request: The current request.

        Returns:
            The rendered diagnostics page.
        """
        from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
        from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
        from urbanlens.dashboard.services.labels.redata_suggestions import redata_labels_configured

        if not redata_labels_configured():
            return render(
                request,
                "dashboard/pages/site_admin_models.html",
                {"page_name": "site-admin-models", "configured": False},
            )

        return render(
            request,
            "dashboard/pages/site_admin_models.html",
            {
                "page_name": "site-admin-models",
                "configured": True,
                # A list so the template renders both identically; adding a
                # third model should not mean a third copy of the markup.
                "models": [
                    {"title": "Label suggestion", "summary": _model_summary(RedataLabelsGateway().get_model, "label-suggestion")},
                    {"title": "Photo relevance", "summary": _model_summary(RedataPhotosGateway().get_model, "photo-relevance")},
                ],
            },
        )
