"""External-API routes for pin surfaces beyond the core CRUD and sync contract.

``urls.py`` owns the sync-critical pin surface - the delta feed (``pins/``),
tombstones (``pins/deleted/``), pin detail, and the sub-resources a client must
have to render a pin offline (notes, aliases, links, visits, comments, review).
That set is deliberately frozen: native clients build their local schema from
it, so it changes on a version bump, not on a Tuesday.

Everything else about a pin belongs here - relationships between pins (child
pins, merge suggestions, duplicate handling), derived or computed views of a
pin, per-pin sharing and exposure, and the long tail of pin actions that have
no home in another domain. Anything that creates a new way to reveal a pin to
someone else must call ``resolve_origin_share`` and ``record_share_exposure``,
or the ``LocationExposure`` provenance chain silently develops a hole and the
site loses its answer to "who could have seen this location, and how".

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

from urbanlens.dashboard.external_api import views_pin_article, views_pin_bulk, views_pin_comments, views_pin_shares, views_pin_sync

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    # Multi-select actions from the main map's bulk toolbar. "bulk" is a
    # literal that a pin slug would otherwise swallow whole, same hazard as
    # "deleted" in urls.py's core pin routes - order_by_specificity protects
    # it regardless of where this module ends up in the concatenated list.
    path("pins/bulk/delete/", views_pin_bulk.PinBulkDeleteView.as_view(), name="pins.bulk.delete"),
    path("pins/bulk/merge/", views_pin_bulk.PinBulkMergeView.as_view(), name="pins.bulk.merge"),
    path("pins/bulk/edit/", views_pin_bulk.PinBulkEditView.as_view(), name="pins.bulk.edit"),
    # Emoji reactions on a pin's own comment thread. ``pins:write``, matching
    # the comment list/create/delete routes in ``urls.py``, not ``wiki:write``:
    # a pin's thread is the owner's private annotation of their own pin.
    path("pins/<str:pin_slug>/comments/<int:comment_id>/reactions/<str:emoji>/", views_pin_comments.PinCommentReactionView.as_view(), name="pins.comments.reactions"),
    # A pin's own long-form article. Same shape as the wiki article routes in
    # ``urls.py`` and deliberately *not* the same scopes - see
    # ``views_pin_article``'s docstring on why ``wiki:*`` here would be a
    # privacy bug. "article" and "revisions" are literals that a pin slug
    # cannot swallow, and ``order_by_specificity`` guarantees that regardless
    # of where these end up in the combined list.
    path("pins/<str:pin_slug>/article/", views_pin_article.PinArticleView.as_view(), name="pins.article"),
    path("pins/<str:pin_slug>/article/revisions/", views_pin_article.PinArticleRevisionsView.as_view(), name="pins.article.revisions"),
    path("pins/<str:pin_slug>/article/revisions/<int:revision_id>/", views_pin_article.PinArticleRevisionDetailView.as_view(), name="pins.article.revisions.detail"),
    path("pins/<str:pin_slug>/article/revisions/<int:revision_id>/restore/", views_pin_article.PinArticleRevisionRestoreView.as_view(), name="pins.article.revisions.restore"),
    # Manual pin <-> wiki child-marker sync. Both carry a stacked resync
    # throttle; push is O(children x buildings) with a GEOS test per pair.
    path("pins/<str:pin_slug>/wiki-sync/push/", views_pin_sync.PinWikiSyncPushView.as_view(), name="pins.wiki-sync.push"),
    path("pins/<str:pin_slug>/wiki-sync/pull/", views_pin_sync.PinWikiSyncPullView.as_view(), name="pins.wiki-sync.pull"),
    # Answering a share someone sent you. Deliberately *not* nested under
    # ``pins/`` - the caller does not own the pin being offered, and hanging it
    # off the owner-scoped pin prefix would invite a future reader to resolve
    # it with ``OwnedPinMixin`` and break the endpoint. Scoped by
    # ``to_profile`` in the view; ``pins:write`` rather than ``messages:*``
    # because the same share also arrives by bare notification.
    path("pin-shares/<int:share_id>/respond/", views_pin_shares.PinShareRespondView.as_view(), name="pin-shares.respond"),
]
