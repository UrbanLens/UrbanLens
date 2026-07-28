"""External-API routes for standalone tools and bulk operations.

Owns the endpoints that *act on* data without owning any of it: import and
export (KML/GPX/CSV/document ingestion), bulk edits applied across a selection,
map and geometry helpers, and the one-off utilities a client needs but that
belong to no single resource. If an endpoint's natural noun is a verb, it
probably lives here.

Two constraints shape this domain. First, almost everything here is slow enough
to need Celery plus a progress indicator and a completion toast on the client,
so routes should be job-shaped - start a job, poll it, fetch its result - rather
than a single request that blocks until a 20,000-pin export finishes. Second,
bulk endpoints are the classic place for authorization to be checked once for
the batch instead of once per object; every operation here must filter the
target set down to what the caller may actually touch before it does anything,
not trust the identifiers it was handed.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = []
