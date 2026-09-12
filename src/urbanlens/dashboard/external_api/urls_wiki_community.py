"""External-API routes for the community-governance side of location wikis.

Every route here inherits the same non-negotiable rule as ``views_wiki``: resolution goes through
``services.wiki.wiki_access.resolve_visible_wiki``, so a wiki the caller has not earned access to is
a 404 rather than a 403.
A 403 would confirm the location exists, which is precisely the disclosure the earned-access model
is there to prevent - and governance endpoints are the easiest place to leak it by accident, because
they are naturally written as "is this person allowed to do X" checks.
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
    # Which of a place's names is *the* name is a community decision, not a content edit - it is recorded in the
    # wiki's edit history and is revertible from there like any other governance action, which is why the route
    # lives here rather than beside the alias list in urls.py.
    path("wikis/<str:location_slug>/aliases/<int:alias_id>/use/", views_wiki.WikiAliasUseView.as_view(), name="wikis.aliases.use"),
    # A display preference on shared data, not a rename - alongside alias-use
    # for the same reason: both act on a WikiAlias row every pinner shares.
    path("wikis/<str:location_slug>/aliases/<int:alias_id>/toggle-nickname/", views_wiki.WikiAliasToggleNicknameView.as_view(), name="wikis.aliases.toggle_nickname"),
    # Community boundary proposals, per this module's own docstring above.
    path("wikis/<str:location_slug>/boundary/", views_wiki.WikiBoundaryApiView.as_view(), name="wikis.boundary"),
]
