"""Shared UI fragments that are the same for every user and every page.

A fragment belongs here when it is derived from module constants rather than
from the request: it can then be rendered once per process, cached in the
browser under a content-hashed URL, and fetched once instead of being rendered
into every widget that needs it.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, HttpResponseNotModified
from django.views import View

from urbanlens.dashboard.services.core.icon_grid import icon_grid_html, icon_grid_version

#: A year, the conventional ceiling for an immutable response.
IMMUTABLE_MAX_AGE = 31_536_000


class IconPickerGridView(LoginRequiredMixin, View):
    """The icon picker's catalogue of buttons, shared by every picker on every page.

    GET /dashboard/ui/icon-picker-grid/?v=<hash>

    Rendering this inline is what made the achievement admin cost tens of
    megabytes per page load (P68). The response carries no picker id and no
    per-user data, so one cached copy serves the whole session.

    ``private`` rather than ``public`` despite being identical for everyone: the
    view is behind ``LoginRequiredMixin``, and a shared cache holding a response
    to an authenticated request is a habit worth not forming. Only a request
    whose ``v`` matches the current catalogue is cached immutably - a stale or
    absent version revalidates, so a link written by an older page can never
    pin the browser to icons this deployment no longer has.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Serve the grid, with caching keyed to the catalogue's content hash."""
        version = icon_grid_version()
        etag = f'"{version}"'
        if request.headers.get("If-None-Match") == etag:
            not_modified = HttpResponseNotModified()
            not_modified["ETag"] = etag
            return not_modified

        response = HttpResponse(icon_grid_html(), content_type="text/html; charset=utf-8")
        response["ETag"] = etag
        response["Cache-Control"] = f"private, max-age={IMMUTABLE_MAX_AGE}, immutable" if request.GET.get("v") == version else "private, no-cache"
        return response
