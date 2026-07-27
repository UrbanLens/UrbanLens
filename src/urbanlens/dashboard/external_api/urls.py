"""URL routes for the external API, mounted at ``dashboard/api/external/v1/``.

Versioned and namespaced separately from the internal REST surface
(``dashboard/rest/``, see ``dashboard/urls.py``) because this one has a
public consumer contract - a third-party application holding a user's API
key - that the internal API doesn't.
"""

from __future__ import annotations

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from urbanlens.dashboard.external_api import views, views_wiki

app_name = "external_api"

urlpatterns = [
    path("whoami/", views.WhoAmIView.as_view(), name="whoami"),
    path("auth/session/", views.AuthSessionView.as_view(), name="auth.session"),
    path("settings/", views.AccountSettingsView.as_view(), name="settings"),
    path("pins/", views.PinsView.as_view(), name="pins"),
    path("pins/deleted/", views.PinTombstonesView.as_view(), name="pins.deleted"),
    # Must stay after the two literal "pins/..." paths above - Django matches
    # urlpatterns in order, and this generic slug segment would otherwise
    # swallow both of them.
    # Pin sub-resources. These sit after the literal paths above for the same
    # ordering reason, but are safe alongside "pins/<str:pin_slug>/" itself:
    # a <str:> converter never matches a "/", so "pins/x/comments/" cannot be
    # swallowed by the single-segment pin-detail pattern.
    path("pins/<str:pin_slug>/comments/", views_wiki.PinCommentsView.as_view(), name="pins.comments"),
    path("pins/<str:pin_slug>/comments/<int:comment_id>/", views_wiki.PinCommentDetailView.as_view(), name="pins.comments.detail"),
    path("pins/<str:pin_slug>/review/", views_wiki.PinReviewView.as_view(), name="pins.review"),
    path("pins/<str:pin_slug>/", views.PinDetailView.as_view(), name="pins.detail"),
    path("pin-suggestions/", views.PinSuggestionsView.as_view(), name="pin_suggestions"),
    # Community wikis. Every one of these resolves through
    # services.wiki_access.resolve_visible_wiki - see views_wiki's module
    # docstring for the anti-enumeration guarantee that depends on it.
    path("wikis/<str:location_slug>/", views_wiki.WikiDetailApiView.as_view(), name="wikis.detail"),
    path("wikis/<str:location_slug>/history/", views_wiki.WikiHistoryView.as_view(), name="wikis.history"),
    path("wikis/<str:location_slug>/history/<int:edit_id>/revert/", views_wiki.WikiRevertView.as_view(), name="wikis.history.revert"),
    path("wikis/<str:location_slug>/votes/<str:field>/", views_wiki.WikiStatVoteApiView.as_view(), name="wikis.votes"),
    path("wikis/<str:location_slug>/aliases/", views_wiki.WikiAliasesView.as_view(), name="wikis.aliases"),
    path("wikis/<str:location_slug>/aliases/<int:alias_id>/", views_wiki.WikiAliasDetailView.as_view(), name="wikis.aliases.detail"),
    path("wikis/<str:location_slug>/links/", views_wiki.WikiLinksView.as_view(), name="wikis.links"),
    path("wikis/<str:location_slug>/links/<int:link_id>/", views_wiki.WikiLinkDetailView.as_view(), name="wikis.links.detail"),
    path("wikis/<str:location_slug>/gallery/", views_wiki.WikiGalleryView.as_view(), name="wikis.gallery"),
    path("wikis/<str:location_slug>/article/", views_wiki.WikiArticleView.as_view(), name="wikis.article"),
    path("wikis/<str:location_slug>/article/revisions/", views_wiki.WikiArticleRevisionsView.as_view(), name="wikis.article.revisions"),
    path("wikis/<str:location_slug>/article/revisions/<int:revision_id>/", views_wiki.WikiArticleRevisionDetailView.as_view(), name="wikis.article.revisions.detail"),
    path("wikis/<str:location_slug>/article/revisions/<int:revision_id>/restore/", views_wiki.WikiArticleRevisionRestoreView.as_view(), name="wikis.article.revisions.restore"),
    path("wikis/<str:location_slug>/comments/", views_wiki.WikiCommentsView.as_view(), name="wikis.comments"),
    path("wikis/<str:location_slug>/comments/<int:comment_id>/", views_wiki.WikiCommentDetailView.as_view(), name="wikis.comments.detail"),
    path("wikis/<str:location_slug>/comments/<int:comment_id>/reactions/<str:emoji>/", views_wiki.WikiCommentReactionView.as_view(), name="wikis.comments.reactions"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
