"""External-API routes for wiki-attached data that has no controller of its own.

``urls.py`` already routes a wiki's core content (detail, article, aliases,
links, gallery, comments), and ``urls_wiki_community.py`` owns how that
content is governed (boundary proposals, name promotion). This module is the
third bucket: data that hangs off a wiki but was never wired into a template
view internally - ``WikiOwner``/``WikiPropertySale`` have full models and
querysets (``models/property_owner/``) but no controller anywhere, and the
cover photo lives on a plain HTMX endpoint rather than a REST one
(``controllers.image_gallery.WikiCoverPhotoView``).

Every route here still goes through ``services.wiki_access.resolve_visible_wiki``
via ``WikiApiView.resolve`` - see ``views_wiki``'s module docstring for why a
wiki the caller has not earned access to must be a 404, never a 403.

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

from urbanlens.dashboard.external_api import views_wiki

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    path("wikis/<str:location_slug>/cover-photo/", views_wiki.WikiCoverPhotoApiView.as_view(), name="wikis.cover_photo"),
    path("wikis/<str:location_slug>/ownership/", views_wiki.WikiOwnershipView.as_view(), name="wikis.ownership"),
    path("wikis/<str:location_slug>/sales/", views_wiki.WikiPropertySalesView.as_view(), name="wikis.sales"),
]
