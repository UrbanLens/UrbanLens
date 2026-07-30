"""External-API routes for search across UrbanLens's own data.

Owns global and per-type search over the things a caller can already reach -
pins, wikis, profiles, trips, lists, labels and photos - plus the suggestion and
type-ahead endpoints built on them.

Not to be confused with ``locations/search/`` and ``locations/resolve/`` in
``urls.py``: those query external geocoders and place providers to turn a phrase
into a coordinate, and they are metered third-party calls. Search here never
leaves the database.

Search is the highest-risk surface in this API for accidental disclosure,
because a result set is built by *matching* rather than by *fetching a thing the
caller named*. Every query has to be filtered by visibility at the queryset
level rather than after the fact, or a private pin leaks through its own title
in an autocomplete dropdown - and a result count alone is enough to confirm that
a location exists.

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

from django.urls import path

from urbanlens.dashboard.external_api import views_search

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    path("search/", views_search.GlobalSearchView.as_view(), name="search"),
]
