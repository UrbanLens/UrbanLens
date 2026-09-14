"""External-API routes for standalone tools and bulk operations.

First, almost everything here is slow enough to need Celery plus a progress indicator and a
completion toast on the client, so routes should be job-shaped - start a job, poll it, fetch its
result - rather than a single request that blocks until a 20,000-pin export finishes.
Second, bulk endpoints are the classic place for authorization to be checked once for the batch
instead of once per object; every operation here must filter the target set down to what the caller
may actually touch before it does anything, not trust the identifiers it was handed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_undo

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Undo lives here rather than in its own module: it is a utility belonging
#: to no single resource, aggregating across every model the undo framework can restore - exactly the shape this
#: domain's docstring describes.
urlpatterns: list[URLPattern] = [
    path("undo/", views_undo.UndoListView.as_view(), name="undo"),
    path("undo/<uuid:undo_uuid>/restore/", views_undo.UndoRestoreView.as_view(), name="undo.restore"),
]
