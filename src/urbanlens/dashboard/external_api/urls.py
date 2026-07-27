"""URL routes for the external API, mounted at ``dashboard/api/external/v1/``.

Versioned and namespaced separately from the internal REST surface
(``dashboard/rest/``, see ``dashboard/urls.py``) because this one has a
public consumer contract - a third-party application holding a user's API
key - that the internal API doesn't.
"""

from __future__ import annotations

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from urbanlens.dashboard.external_api import views, views_messaging, views_wiki

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
    # A pin's sub-resources. These sit after the detail route above only for
    # readability - each has a literal trailing segment, so none of them can be
    # shadowed by it.
    path("pins/<str:pin_slug>/notes/", views.PinNotesView.as_view(), name="pins.notes"),
    path("pins/<str:pin_slug>/notes/<int:note_id>/", views.PinNoteDetailView.as_view(), name="pins.notes.detail"),
    path("pins/<str:pin_slug>/aliases/", views.PinAliasesView.as_view(), name="pins.aliases"),
    path("pins/<str:pin_slug>/aliases/<int:alias_id>/", views.PinAliasDetailView.as_view(), name="pins.aliases.detail"),
    path("pins/<str:pin_slug>/aliases/<int:alias_id>/use/", views.PinAliasUseView.as_view(), name="pins.aliases.use"),
    path("pins/<str:pin_slug>/links/", views.PinLinksView.as_view(), name="pins.links"),
    path("pins/<str:pin_slug>/links/<int:link_id>/", views.PinLinkDetailView.as_view(), name="pins.links.detail"),
    path("pins/<str:pin_slug>/visits/", views.PinVisitsView.as_view(), name="pins.visits"),
    path("pins/<str:pin_slug>/visits/<int:visit_id>/", views.PinVisitDetailView.as_view(), name="pins.visits.detail"),
    path("locations/search/", views.LocationSearchView.as_view(), name="locations.search"),
    path("locations/resolve/", views.PlaceResolveView.as_view(), name="locations.resolve"),
    path("pin-suggestions/", views.PinSuggestionsView.as_view(), name="pin_suggestions"),
    path("photos/", views.PhotosView.as_view(), name="photos"),
    # The <uuid:...> converter only matches a well-formed uuid, so these can't
    # shadow the literal "photos/" route above - but they stay after it to
    # keep this file's literal-before-generic ordering readable.
    path("photos/<uuid:image_uuid>/", views.PhotoDetailView.as_view(), name="photos.detail"),
    path("photos/<uuid:image_uuid>/labels/", views.PhotoLabelsView.as_view(), name="photos.labels"),
    path("photos/<uuid:image_uuid>/vote/", views.PhotoVoteView.as_view(), name="photos.vote"),
    path("photos/<uuid:image_uuid>/file/", views.PhotoFileView.as_view(), name="photos.file"),
    path("suggestions/visits/", views.VisitSuggestionsView.as_view(), name="suggestions.visits"),
    path("suggestions/visits/<int:suggestion_id>/<str:action>/", views.VisitSuggestionActionView.as_view(), name="suggestions.visits.action"),
    path("memories/journal/", views.MemoriesJournalView.as_view(), name="memories.journal"),
    path("lists/", views.PinListsView.as_view(), name="lists"),
    # The three literal sub-paths below must all stay ahead of the generic
    # "lists/<slug:list_slug>/" route: Django matches in order, and a list
    # slug would otherwise swallow "items"/"reorder"/"resync" as if they were
    # list identifiers. Within the items group, the literal "reorder/" segment
    # likewise precedes nothing generic, but is kept adjacent for clarity.
    path("lists/<slug:list_slug>/items/reorder/", views.PinListItemsReorderView.as_view(), name="lists.items.reorder"),
    path("lists/<slug:list_slug>/items/", views.PinListItemsView.as_view(), name="lists.items"),
    path("lists/<slug:list_slug>/resync/", views.PinListResyncView.as_view(), name="lists.resync"),
    path("lists/<slug:list_slug>/", views.PinListDetailView.as_view(), name="lists.detail"),
    path("saved-filters/", views.SavedFiltersView.as_view(), name="saved_filters"),
    path("saved-filters/<uuid:filter_uuid>/", views.SavedFilterDetailView.as_view(), name="saved_filters.detail"),
    path("labels/", views.LabelsView.as_view(), name="labels"),
    # Same ordering rule as the lists group above - the two literal sub-paths
    # precede the generic label-detail route. A <uuid> converter would not in
    # fact match "customization", but the ordering is kept explicit so the
    # convention survives a future switch to a looser converter.
    path("labels/<uuid:label_uuid>/customization/", views.LabelCustomizationView.as_view(), name="labels.customization"),
    path("labels/<uuid:label_uuid>/merge/", views.LabelMergeView.as_view(), name="labels.merge"),
    path("labels/<uuid:label_uuid>/", views.LabelDetailView.as_view(), name="labels.detail"),
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
    # Trips. Every literal sub-path below sits *after* the trip slug segment
    # rather than beside it, so unlike "pins/..." above there is nothing here
    # for a generic slug to swallow - "trips/" and "trips/<slug>/" cannot
    # collide, and each deeper segment is a distinct literal.
    path("trips/", views.TripsView.as_view(), name="trips"),
    path("trips/<slug:trip_slug>/", views.TripDetailView.as_view(), name="trips.detail"),
    path("trips/<slug:trip_slug>/map/", views.TripMapView.as_view(), name="trips.map"),
    path("trips/<slug:trip_slug>/join/", views.TripJoinView.as_view(), name="trips.join"),
    path("trips/<slug:trip_slug>/leave/", views.TripLeaveView.as_view(), name="trips.leave"),
    path("trips/<slug:trip_slug>/rsvp/", views.TripRsvpView.as_view(), name="trips.rsvp"),
    path("trips/<slug:trip_slug>/calendar-sync/", views.TripCalendarSyncView.as_view(), name="trips.calendar_sync"),
    path("trips/<slug:trip_slug>/members/", views.TripMembersView.as_view(), name="trips.members"),
    path("trips/<slug:trip_slug>/members/<slug:member_slug>/", views.TripMemberDetailView.as_view(), name="trips.members.detail"),
    path("trips/<slug:trip_slug>/activities/", views.TripActivitiesView.as_view(), name="trips.activities"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/", views.TripActivityDetailView.as_view(), name="trips.activities.detail"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/position/", views.TripActivityPositionView.as_view(), name="trips.activities.position"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/vote/", views.TripActivityVoteView.as_view(), name="trips.activities.vote"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/status/", views.TripActivityStatusView.as_view(), name="trips.activities.status"),
    path("trips/<slug:trip_slug>/activities/<int:activity_id>/rsvp/", views.TripActivityRsvpView.as_view(), name="trips.activities.rsvp"),
    path("trips/<slug:trip_slug>/comments/", views.TripCommentsView.as_view(), name="trips.comments"),
    path("trips/<slug:trip_slug>/comments/<int:comment_id>/", views.TripCommentDetailView.as_view(), name="trips.comments.detail"),
    path("trips/<slug:trip_slug>/comments/<int:comment_id>/reactions/", views.TripCommentReactionsView.as_view(), name="trips.comments.reactions"),
    path("push-devices/", views.PushDevicesView.as_view(), name="push_devices"),
    path("push-devices/<uuid:device_uuid>/", views.PushDeviceDetailView.as_view(), name="push_devices.detail"),
    path("friends/", views.FriendsView.as_view(), name="friends"),
    path("friends/<uuid:profile_uuid>/", views.FriendDetailView.as_view(), name="friends.detail"),
    path("friends/<uuid:profile_uuid>/accept/", views.FriendAcceptView.as_view(), name="friends.accept"),
    path("friends/<uuid:profile_uuid>/reject/", views.FriendRejectView.as_view(), name="friends.reject"),
    path("friends/<uuid:profile_uuid>/ignore/", views.FriendIgnoreView.as_view(), name="friends.ignore"),
    path("friends/<uuid:profile_uuid>/block/", views.FriendBlockView.as_view(), name="friends.block"),
    path("friends/<uuid:profile_uuid>/mute/", views.FriendMuteView.as_view(), name="friends.mute"),
    path("friend-invites/", views.FriendInvitesView.as_view(), name="friend_invites"),
    path("notifications/", views.NotificationsView.as_view(), name="notifications"),
    # These two literal paths must stay ahead of the <uuid:notification_uuid>
    # route below - Django matches urlpatterns in order, and although a uuid
    # converter would not in fact match "read-all", keeping the literals first
    # follows this file's existing convention (see the "pins/..." block) and
    # keeps the ordering correct if the converter is ever loosened to <str:>.
    path("notifications/read-all/", views.NotificationsReadAllView.as_view(), name="notifications.read_all"),
    path("notifications/unread-count/", views.NotificationsUnreadCountView.as_view(), name="notifications.unread_count"),
    path("notifications/<uuid:notification_uuid>/", views.NotificationDetailView.as_view(), name="notifications.detail"),
    path("notification-preferences/", views.NotificationDeliveryPreferencesView.as_view(), name="notification_preferences"),
    # Kept after the two literal "profiles/..." sub-resources would-be conflicts:
    # the notes routes are more specific paths under the same slug segment, so
    # they are declared before the bare profile detail route.
    path("profiles/<str:profile_slug>/notes/", views.ProfileNotesView.as_view(), name="profiles.notes"),
    path("profiles/<str:profile_slug>/notes/<uuid:note_uuid>/", views.ProfileNoteDetailView.as_view(), name="profiles.notes.detail"),
    path("profiles/<str:profile_slug>/", views.ProfileDetailView.as_view(), name="profiles.detail"),
    # Messaging. Every literal "messages/..." path MUST stay above the
    # "messages/<str:peer_slug>/" routes below: Django matches in order, and a
    # profile whose slug happened to be "settings", "groups" or
    # "conversations" would otherwise shadow the endpoint of that name (or,
    # worse, be shadowed by it). views_messaging.RESERVED_PEER_SLUGS refuses
    # those slugs as peers as well, so the two defenses have to both fail
    # before a request can be misrouted.
    path("messages/conversations/", views_messaging.ConversationsView.as_view(), name="messages.conversations"),
    path("messages/settings/", views_messaging.MessageSettingsView.as_view(), name="messages.settings"),
    path("messages/groups/", views_messaging.GroupsView.as_view(), name="messages.groups"),
    path("messages/groups/<uuid:group_uuid>/", views_messaging.GroupDetailView.as_view(), name="messages.groups.detail"),
    path("messages/groups/<uuid:group_uuid>/messages/", views_messaging.GroupMessagesView.as_view(), name="messages.groups.messages"),
    path("messages/groups/<uuid:group_uuid>/read/", views_messaging.GroupReadView.as_view(), name="messages.groups.read"),
    path("messages/groups/<uuid:group_uuid>/members/", views_messaging.GroupMembersView.as_view(), name="messages.groups.members"),
    path("messages/groups/<uuid:group_uuid>/share/pin/", views_messaging.GroupPinShareView.as_view(), name="messages.groups.share.pin"),
    # Peer-slug routes - the catch-all segment, hence last. The more specific
    # sub-paths still precede the bare thread route for the same reason.
    path("messages/<str:peer_slug>/read/", views_messaging.MessageThreadReadView.as_view(), name="messages.read"),
    path("messages/<str:peer_slug>/react/<int:message_id>/", views_messaging.MessageReactionView.as_view(), name="messages.react"),
    path("messages/<str:peer_slug>/messages/<int:message_id>/", views_messaging.MessageDetailView.as_view(), name="messages.detail"),
    path("messages/<str:peer_slug>/", views_messaging.MessageThreadView.as_view(), name="messages.thread"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]
