from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.services.pins.external_data import PanelSource


class PinPanelViewMixin:
    """
    The plumbing a Private Pin page's external-data panel view shares: debug entries, polling placeholders and refresh events.

    External-data panels never fetch upstream data on the request path: on a store miss the view schedules a Celery
    task (single-flight) and returns a self-polling placeholder; polls re-enter the same endpoint with ``?attempt=N``
    until the task lands the data or the attempt budget runs out.
    """

    def _debug_entry(self, request: HttpRequest, source: str, query: str, *, from_cache: bool, count: int | None = None):
        """
        Build a `DebugEntry` for the external-API debug overlay, admins only.

        Args:
            request: The current HttpRequest.
            source: Short identifier for the data source (e.g. ``"wikipedia"``).
            query: The search term, address, or coordinates used for the lookup.
            from_cache: Whether the result was served from cache.
            count: Number of results the lookup produced, when meaningful.

        Returns:
            A `DebugEntry`, or None if the requesting user can't view debug info.
        """
        from urbanlens.dashboard.services.admin.debug_overlay import DebugEntry, can_view_debug_overlay

        if not can_view_debug_overlay(request.user):
            return None
        return DebugEntry(source=source, query=query, from_cache=from_cache, count=count)

    @staticmethod
    def _poll_attempt(request: HttpRequest) -> int:
        """Which poll cycle this request is (0 for the initial page-load request)."""
        try:
            return max(int(request.GET.get("attempt", "0")), 0)
        except (TypeError, ValueError):
            return 0

    def _pending_panel(self, request: HttpRequest, pin: Pin, source_key: str, hide_tab_id: str | None = None, section_id: str | None = None, *, in_tab: bool = False):
        """
        Schedule a panel fetch and return its polling placeholder.

        Args:
            request: The current request.
            pin: The pin whose panel data is being fetched.
            source_key: An ``external_data.panel_sources()`` key.
            hide_tab_id: DOM id to hide on 204, if any.
            section_id: The placeholder's DOM id, when not the source's own.
            in_tab: Whether the placeholder fills a tab body - see :meth:`_pending_placeholder`.

        Returns:
            The placeholder fragment; a note when this environment does not call the panel's source; else 204 when
            suppressed or exhausted.
        """
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, get_panel_source, schedule_panel_fetch, unavailable_here

        source = get_panel_source(source_key)
        if source is not None and unavailable_here(source_key, pin):
            return self._unavailable_here(request, source, section_id=section_id or source.section_id, in_tab=in_tab)
        attempt = self._poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(source_key, pin):
            return HttpResponse(status=204)
        if source is None:
            return HttpResponse(status=204)
        return self._pending_placeholder(request, source, section_id=section_id or source.section_id, in_tab=in_tab, hide_tab_id=hide_tab_id)

    @staticmethod
    def _unavailable_here(request: HttpRequest, source: PanelSource, *, section_id: str, in_tab: bool) -> HttpResponse:
        """
        Say a panel's source is not called from this environment, rather than showing it empty or hiding it.

        Args:
            request: The current request.
            source: The panel whose source was refused.
            section_id: The fragment's DOM id.
            in_tab: Whether it fills a tab body.

        Returns:
            The note fragment. Never polls: nothing is in flight.
        """
        return render(
            request,
            "dashboard/partials/pins/panel_unavailable_here.html",
            {
                "section_id": section_id,
                "outer_class": source.outer_class,
                "outer_is_card": source.outer_is_card,
                "icon": source.icon,
                "title": source.title,
                "in_tab": in_tab,
            },
        )

    def _pending_placeholder(self, request: HttpRequest, source: PanelSource, *, section_id: str, in_tab: bool, title: str | None = None, hide_tab_id: str | None = None) -> HttpResponse:
        """
        The self-polling placeholder for a panel whose fetch is in flight.

        Args:
            request: The current request; its path is polled.
            source: The panel being fetched.
            section_id: The placeholder's DOM id.
            in_tab: Whether it fills a tab body, where it is the tab's only content and so shows its spinner. A card of
                its own stays hidden until there is something to show.
            title: The heading, when not the source's own title.
            hide_tab_id: DOM id to hide on 204, if any.

        Returns:
            The placeholder fragment.
        """
        from urbanlens.dashboard.services.pins.external_data import POLL_INTERVAL_SECONDS

        return render(
            request,
            "dashboard/partials/pins/panel_pending.html",
            {
                "section_id": section_id,
                "outer_class": source.outer_class,
                "outer_is_card": source.outer_is_card,
                "quiet_on_error": source.quiet_on_error,
                "icon": source.icon,
                "title": title or source.title,
                "poll_url": request.path,
                "next_attempt": self._poll_attempt(request) + 1,
                "poll_interval": POLL_INTERVAL_SECONDS,
                "hide_tab_id": hide_tab_id,
                "in_tab": in_tab,
            },
        )

    @staticmethod
    def _notify_panel_ready(request: HttpRequest, response: HttpResponse, *events: str) -> HttpResponse:
        """
        Notify sibling panels to refresh via HX-Trigger.

        Args:
            request: The current request.
            response: The response to annotate.
            *events: Client event names other panels listen for.

        Returns:
            The same response, for chaining.
        """
        if PinPanelViewMixin._poll_attempt(request) < 1:
            return response
        response["HX-Trigger"] = json.dumps(dict.fromkeys(events, True))
        return response
