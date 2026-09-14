"""External-API routes for pin surfaces beyond the core CRUD and sync contract.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat ``external_api:`` namespace
and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so declaration order inside
this module only breaks ties between routes of identical shape.
Use ``path()`` (``re_path()`` cannot be ordered and is rejected at import time) and keep every
``name=`` unique across the whole external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_pin_article, views_pin_bulk, views_pin_comments, views_pin_shares, views_pin_sync

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    # Multi-select actions from the main map's bulk toolbar.
    path("pins/bulk/delete/", views_pin_bulk.PinBulkDeleteView.as_view(), name="pins.bulk.delete"),
    path("pins/bulk/merge/", views_pin_bulk.PinBulkMergeView.as_view(), name="pins.bulk.merge"),
    path("pins/bulk/edit/", views_pin_bulk.PinBulkEditView.as_view(), name="pins.bulk.edit"),
    # Emoji reactions on a pin's own comment thread.
    path("pins/<str:pin_slug>/comments/<int:comment_id>/reactions/<str:emoji>/", views_pin_comments.PinCommentReactionView.as_view(), name="pins.comments.reactions"),
    # A pin's own long-form article. Same shape as the wiki article routes in ``urls.py`` and deliberately *not*
    # the same scopes - see ``views_pin_article``'s docstring on why ``wiki:*`` here would be a privacy bug.
    path("pins/<str:pin_slug>/article/", views_pin_article.PinArticleView.as_view(), name="pins.article"),
    path("pins/<str:pin_slug>/article/revisions/", views_pin_article.PinArticleRevisionsView.as_view(), name="pins.article.revisions"),
    path("pins/<str:pin_slug>/article/revisions/<int:revision_id>/", views_pin_article.PinArticleRevisionDetailView.as_view(), name="pins.article.revisions.detail"),
    path("pins/<str:pin_slug>/article/revisions/<int:revision_id>/restore/", views_pin_article.PinArticleRevisionRestoreView.as_view(), name="pins.article.revisions.restore"),
    # Manual pin <-> wiki child-marker sync. Both carry a stacked resync
    # throttle; push is O(children x buildings) with a GEOS test per pair.
    path("pins/<str:pin_slug>/wiki-sync/push/", views_pin_sync.PinWikiSyncPushView.as_view(), name="pins.wiki-sync.push"),
    path("pins/<str:pin_slug>/wiki-sync/pull/", views_pin_sync.PinWikiSyncPullView.as_view(), name="pins.wiki-sync.pull"),
    # Answering a share someone sent you. Scoped by ``to_profile`` in the view; ``pins:write`` rather than
    # ``messages:*`` because the same share also arrives by bare notification.
    path("pin-shares/<int:share_id>/respond/", views_pin_shares.PinShareRespondView.as_view(), name="pin-shares.respond"),
]
