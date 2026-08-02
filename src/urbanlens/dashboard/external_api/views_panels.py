"""External-facing pin-detail panel endpoints: enrichment data as JSON.

Exposes exactly the panels that have opted in via a non-empty
``PanelSource.api_kinds`` - see ``services.pins.external_data`` for why that,
rather than whether ``api_payload`` happens to return data right now, is the
authoritative "exposed to the API" signal. The satellite and street-view
carousels are deliberately not among them: their web payload is base64
``data:`` URIs (5-15MB/response), and this API's throttle counts requests, not
bytes, so exposing them would let one throttled-by-count credential become an
unmetered bandwidth amplifier. Fixing that needs a signed slide-image proxy
that doesn't exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_panels import PanelListEntrySerializer, PanelPendingSerializer
from urbanlens.dashboard.external_api.views import ExternalApiView, OwnedPinMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.pins.external_data import POLL_INTERVAL_SECONDS, get_panel_source, panel_readiness, panel_sources, panel_visible_to, schedule_panel_fetch

if TYPE_CHECKING:
    from rest_framework.request import Request

#: Retry hint when a fetch could not be scheduled (unknown-to-scheduler,
#: suppressed after a recent failure, or the Celery broker was unreachable) -
#: deliberately longer than the in-flight case, since none of those resolve
#: on the next couple of polls the way an in-flight fetch does.
_POLL_AFTER_SUPPRESSED_SECONDS = 60


class PinPanelsListView(OwnedPinMixin, ExternalApiView):
    """GET: every API-exposed panel available on this pin, with its readiness.

    Only panels with non-empty ``api_kinds`` are listed. A source gated by
    ``gate(pin)`` (e.g. no coordinates to work with) or by a subscription
    feature the caller's account lacks is omitted entirely rather than merely
    marked unready - the same rule the web tab strip applies
    (``controllers.pin._viewer_may_see_panel``, backed by the same
    ``panel_visible_to``).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PANELS_READ}),
    }

    @extend_schema(responses={200: PanelListEntrySerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """List the pin's API-exposed panels and their readiness."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        exposed = sorted(
            (source for source in panel_sources().values() if source.api_kinds and source.gate(pin) and panel_visible_to(request.user, source)),
            key=lambda source: source.key,
        )
        readiness = panel_readiness(pin, exposed)
        entries = [{"key": source.key, "kinds": sorted(kind.value for kind in source.api_kinds), "ready": readiness.get(source.key, False)} for source in exposed]
        return Response(PanelListEntrySerializer(entries, many=True).data)


class PinPanelDetailView(OwnedPinMixin, ExternalApiView):
    """GET: one panel's data if ready; otherwise schedule a fetch and answer 202."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PANELS_READ}),
    }

    @extend_schema(responses={200: None, 202: PanelPendingSerializer, 204: None, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str, panel_key: str) -> Response:
        """Return the panel's data if ready, else schedule a fetch and answer 202."""
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            return Response({"error": "No such pin."}, status=404)

        source = get_panel_source(panel_key)
        if source is None or not source.api_kinds or not source.gate(pin) or not panel_visible_to(request.user, source):
            # Same anti-enumeration policy as the rest of this API: a
            # feature-gated panel the caller can't see must read identically
            # to one that doesn't exist, never a 403.
            return Response({"error": "No such panel."}, status=404)

        if source.is_ready(pin):
            payload = source.api_payload(pin)
            if payload is not None:
                return Response(payload)
            # Landed-but-nothing-there is a real answer for some sources (a
            # media search that found zero results); the JSON equivalent of
            # the web panel's quiet 204.
            return Response(status=204)

        if schedule_panel_fetch(panel_key, pin):
            return Response({"ready": False, "poll_after_seconds": POLL_INTERVAL_SECONDS}, status=202)
        return Response({"ready": False, "poll_after_seconds": _POLL_AFTER_SUPPRESSED_SECONDS}, status=202)
