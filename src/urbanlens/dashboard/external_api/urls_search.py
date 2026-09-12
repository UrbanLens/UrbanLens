"""External-API routes for search across UrbanLens's own data.

Search here never leaves the database.
Search is the highest-risk surface in this API for accidental disclosure, because a result set is
built by *matching* rather than by *fetching a thing the caller named*.
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
