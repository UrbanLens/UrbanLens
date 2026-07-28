"""URL routes for the external API, mounted at ``dashboard/api/external/v1/``.

Versioned and namespaced separately from the internal REST surface
(``dashboard/rest/``, see ``dashboard/urls.py``) because this one has a public
consumer contract - a third-party application holding a user's API key - that
the internal API doesn't.

Routing is split across the ``urls_*.py`` siblings, one module per domain, so
that many people can add endpoints at once without every change serialising on
this file. Those modules are stitched in by plain list concatenation, *not*
``include()``: every external route has to stay in the single flat
``external_api:`` namespace, because ``reverse("external_api:pins.detail")``
calls are spread across the codebase and ``schema.preprocess_external_api_only``
selects endpoints by URL prefix. An ``include()`` would insert a namespace
segment and break both at once - reverse() with a NoReverseMatch that names a
route which visibly exists.

THE ORDERING RULE
-----------------
Django resolves a request by walking ``urlpatterns`` in order and taking the
first pattern that matches, so a generic segment declared ahead of a literal one
swallows it. This is not a theoretical hazard, and its symptom is actively
misleading: ``pins/deleted/`` sitting behind ``pins/<str:pin_slug>/`` does not
produce "no such route" - it dispatches to the pin-detail view, which looks up a
pin slugged "deleted", fails to find one, and answers 404 for a completely
unrelated reason. Nothing in the response distinguishes that from a genuinely
missing pin, so the bug is close to undebuggable from the outside.

Appending the domain modules onto the end of a hand-ordered list would have made
that the *default* outcome. Any new single-segment literal under a prefix that
already has a generic route - ``pins/``, ``wikis/``, ``trips/``, ``lists/``,
``labels/``, ``messages/``, ``photos/``, ``profiles/``, ``safety/checkins/`` -
would land after the pattern that eats it, and the author, editing only their
own domain module, would have no reason to even look at this file.

So declaration order is deliberately *not* what decides matching here. The
combined list is run through :func:`order_by_specificity`, which re-sorts it by
how narrowly each path segment matches: literals first, then the tightly
constrained converters (``int``, ``uuid``), then ``slug``, then ``str``, then
``path``. Equal keys keep their relative order, because Python's sort is stable,
so a module's internal ordering still decides ties between identically shaped
routes. The invariant that buys us is the one that matters: a literal route can
never be shadowed by a generic one, regardless of which module declared it or in
what order the modules happen to be concatenated.

``tests/hypothesis/test_external_api_url_resolution.py`` holds the line. It
reverses every registered route and resolves the resulting path back, asserting
it lands on the route it came from - which is exactly the assertion a shadowed
pattern fails. It is data-driven off this urlconf, so routes added later are
covered without anyone remembering to extend it.

This file is closed to further edits. Add routes to the ``urls_*.py`` module
that owns your domain instead; if no module fits, say so rather than reopening
this one.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from django.urls import path
from django.urls.resolvers import RoutePattern, URLResolver
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from urbanlens.dashboard.external_api import (
    urls_assistant,
    urls_connections,
    urls_custom_fields,
    urls_games,
    urls_labels_extra,
    urls_lists_extra,
    urls_memories,
    urls_messaging,
    urls_panels,
    urls_pin_extra,
    urls_safety_partner,
    urls_search,
    urls_site,
    urls_social,
    urls_tools,
    urls_trips,
    urls_wiki_community,
    urls_wiki_extra,
    views,
    views_messaging,
    views_wiki,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from django.urls.resolvers import URLPattern

app_name = "external_api"

#: Matches one ``<converter:name>`` (or bare ``<name>``) capture inside a route,
#: using the same grammar Django's own ``_route_to_regex`` parses routes with.
_ROUTE_PARAMETER_RE: Final = re.compile(r"<(?:([^>:]+):)?([^>]+)>")

#: How *broadly* each built-in converter matches, smallest set first. This is a
#: subset ordering, not a preference: ``int`` (``[0-9]+``) and ``uuid`` accept
#: strictly less than ``slug`` (``[-a-zA-Z0-9_]+``), which accepts strictly less
#: than ``str`` (``[^/]+``), which accepts strictly less than ``path`` (which is
#: the only one that will cross a ``/``). Sorting narrow-before-broad therefore
#: means a segment can only ever be claimed by the tightest pattern that fits it.
_CONVERTER_BREADTH: Final[Mapping[str, int]] = {
    "int": 1,
    "uuid": 2,
    "slug": 3,
    "str": 4,
    "path": 5,
}

#: Breadth assumed for a converter we don't recognise - a project-registered one,
#: say. Treating it as ``str`` puts it alongside the broadest single-segment
#: converter, which is the safe assumption: we may order it later than strictly
#: necessary, but never earlier than something it could swallow.
_UNKNOWN_CONVERTER_BREADTH: Final[int] = _CONVERTER_BREADTH["str"]

#: Breadth of a segment with no captures at all. Zero, because a literal matches
#: exactly one string and so can never be broader than any converter.
_LITERAL_BREADTH: Final[int] = 0


def _segment_specificity(segment: str) -> tuple[int, int]:
    """Rank a single path segment by how broadly it matches.

    Args:
        segment: One ``/``-delimited piece of a ``path()`` route, e.g. ``"pins"``,
            ``"<str:pin_slug>"`` or the empty string a trailing slash leaves behind.

    Returns:
        A ``(breadth, no_literal_text)`` pair, ordered narrowest-first. The
        second element is the tie-break for mixed segments such as
        ``"page-<int:number>"``: a converter fenced in by literal text accepts
        less than the same converter standing alone, so it sorts ahead of it.
    """
    parameters = _ROUTE_PARAMETER_RE.findall(segment)
    if not parameters:
        return (_LITERAL_BREADTH, 0)
    # A segment may legally hold more than one capture; it is only as narrow as
    # its broadest piece, so take the max rather than the first.
    breadth = max(_CONVERTER_BREADTH.get(converter or "str", _UNKNOWN_CONVERTER_BREADTH) for converter, _name in parameters)
    has_literal_text = bool(_ROUTE_PARAMETER_RE.sub("", segment))
    return (breadth, 0 if has_literal_text else 1)


def _route_specificity(route: str) -> tuple[tuple[int, int], ...]:
    """Build the full sort key for a route: its segments' ranks, left to right.

    Comparing these tuples lexicographically implements "leftmost, narrowest
    segment wins", which is the rule a human applies by eye when hand-ordering a
    urlconf. Two routes that share a prefix and differ only in length compare by
    length, and that is harmless: ``path()`` patterns are anchored at both ends,
    so ``pins/<str:pin_slug>/`` cannot match ``pins/x/comments/`` no matter which
    of them is declared first. Only routes with the *same* segment count can
    shadow each other, and for those the segment ranks decide.

    Args:
        route: A route string as passed to ``path()``, with no leading slash -
            e.g. ``"pins/<str:pin_slug>/notes/"``.

    Returns:
        One ``(breadth, no_literal_text)`` pair per segment, in path order.
    """
    return tuple(_segment_specificity(segment) for segment in route.split("/"))


def order_by_specificity(patterns: Sequence[URLPattern]) -> list[URLPattern]:
    """Re-sort a flat list of ``path()`` routes so no literal can be shadowed.

    Django's resolver is first-match-wins over declaration order, which makes a
    urlconf assembled from independently-authored modules unsafe by default: the
    module that happens to be concatenated first silently wins every collision.
    Rather than asking thirteen domain modules to coordinate an ordering none of
    them can see, this recomputes it from the routes themselves - literals ahead
    of converters, narrow converters ahead of broad ones - so the correct order
    is a property of the routes rather than of the concatenation.

    The sort is stable, so routes with identical keys keep their declared
    relative order. That matters for genuinely ambiguous pairs (two ``<str:>``
    routes of the same shape, say): this function will not invent a winner, it
    just preserves the author's. The resolution round-trip test flags those as
    the conflicts they are.

    Args:
        patterns: The concatenated ``urlpatterns`` of every domain module.

    Returns:
        A new list holding the same patterns, ordered narrowest-match-first.

    Raises:
        TypeError: If an entry is not a plain ``path()`` route. ``include()``
            would break the flat ``external_api:`` namespace this API's
            ``reverse()`` calls and schema preprocessing both depend on, and a
            ``re_path()`` regex cannot be decomposed into segments, so neither
            can be ordered - and an unorderable route in this list would quietly
            reintroduce exactly the shadowing this function exists to prevent.
            Failing at import is loud; a silent 404 in production is not.
    """
    for entry in patterns:
        if isinstance(entry, URLResolver):
            raise TypeError(f"external_api route {str(entry.pattern)!r} uses include(); every external route must be a flat path() in the 'external_api:' namespace - concatenate the module's urlpatterns instead")
        if not isinstance(entry.pattern, RoutePattern):
            raise TypeError(f"external_api route {str(entry.pattern)!r} is not a path() route; re_path() cannot be ordered by segment specificity - express the pattern with path() converters")
    return sorted(patterns, key=lambda entry: _route_specificity(str(entry.pattern)))


#: The routes that predate the per-domain split. Frozen for the same reason the
#: module is: the sync-critical surface native clients build their local schema
#: from lives in here. New endpoints go in the domain modules below.
_CORE_URLPATTERNS: list[URLPattern] = [
    path("whoami/", views.WhoAmIView.as_view(), name="whoami"),
    path("auth/session/", views.AuthSessionView.as_view(), name="auth.session"),
    path("settings/", views.AccountSettingsView.as_view(), name="settings"),
    path("pins/", views.PinsView.as_view(), name="pins"),
    # The canonical example of the ordering rule in this module's docstring:
    # "deleted" is a literal that a pin slug would otherwise swallow whole.
    # order_by_specificity now guarantees it wins regardless of where it sits.
    path("pins/deleted/", views.PinTombstonesView.as_view(), name="pins.deleted"),
    path("pins/<str:pin_slug>/comments/", views_wiki.PinCommentsView.as_view(), name="pins.comments"),
    path("pins/<str:pin_slug>/comments/<int:comment_id>/", views_wiki.PinCommentDetailView.as_view(), name="pins.comments.detail"),
    path("pins/<str:pin_slug>/review/", views_wiki.PinReviewView.as_view(), name="pins.review"),
    path("pins/<str:pin_slug>/", views.PinDetailView.as_view(), name="pins.detail"),
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
    path("suggestions/pins/", views.PinSuggestionListApiView.as_view(), name="suggestions.pins"),
    path("suggestions/pins/<int:suggestion_id>/<str:action>/", views.PinSuggestionActionApiView.as_view(), name="suggestions.pins.action"),
    path("photos/", views.PhotosView.as_view(), name="photos"),
    path("photos/<uuid:image_uuid>/", views.PhotoDetailView.as_view(), name="photos.detail"),
    path("photos/<uuid:image_uuid>/labels/", views.PhotoLabelsView.as_view(), name="photos.labels"),
    path("photos/<uuid:image_uuid>/vote/", views.PhotoVoteView.as_view(), name="photos.vote"),
    path("photos/<uuid:image_uuid>/file/", views.PhotoFileView.as_view(), name="photos.file"),
    path("suggestions/visits/", views.VisitSuggestionsView.as_view(), name="suggestions.visits"),
    path("suggestions/visits/<int:suggestion_id>/<str:action>/", views.VisitSuggestionActionView.as_view(), name="suggestions.visits.action"),
    path("memories/journal/", views.MemoriesJournalView.as_view(), name="memories.journal"),
    path("lists/", views.PinListsView.as_view(), name="lists"),
    path("lists/<slug:list_slug>/items/reorder/", views.PinListItemsReorderView.as_view(), name="lists.items.reorder"),
    path("lists/<slug:list_slug>/items/", views.PinListItemsView.as_view(), name="lists.items"),
    path("lists/<slug:list_slug>/resync/", views.PinListResyncView.as_view(), name="lists.resync"),
    path("lists/<slug:list_slug>/", views.PinListDetailView.as_view(), name="lists.detail"),
    path("saved-filters/", views.SavedFiltersView.as_view(), name="saved_filters"),
    path("saved-filters/<uuid:filter_uuid>/", views.SavedFilterDetailView.as_view(), name="saved_filters.detail"),
    path("labels/", views.LabelsView.as_view(), name="labels"),
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
    path("notifications/read-all/", views.NotificationsReadAllView.as_view(), name="notifications.read_all"),
    path("notifications/unread-count/", views.NotificationsUnreadCountView.as_view(), name="notifications.unread_count"),
    path("notifications/<uuid:notification_uuid>/", views.NotificationDetailView.as_view(), name="notifications.detail"),
    path("notification-preferences/", views.NotificationDeliveryPreferencesView.as_view(), name="notification_preferences"),
    path("profiles/<str:profile_slug>/notes/", views.ProfileNotesView.as_view(), name="profiles.notes"),
    path("profiles/<str:profile_slug>/notes/<uuid:note_uuid>/", views.ProfileNoteDetailView.as_view(), name="profiles.notes.detail"),
    path("profiles/<str:profile_slug>/", views.ProfileDetailView.as_view(), name="profiles.detail"),
    # Messaging. The literal "messages/..." routes and the "<str:peer_slug>"
    # ones share a shape, so the specificity sort is what keeps a profile
    # slugged "settings", "groups" or "conversations" from shadowing - or being
    # shadowed by - the endpoint of that name. views_messaging.RESERVED_PEER_SLUGS
    # refuses those slugs as peers as well, so both defenses have to fail before
    # a request can be misrouted.
    path("messages/conversations/", views_messaging.ConversationsView.as_view(), name="messages.conversations"),
    path("messages/settings/", views_messaging.MessageSettingsView.as_view(), name="messages.settings"),
    path("messages/groups/", views_messaging.GroupsView.as_view(), name="messages.groups"),
    path("messages/groups/<uuid:group_uuid>/", views_messaging.GroupDetailView.as_view(), name="messages.groups.detail"),
    path("messages/groups/<uuid:group_uuid>/messages/", views_messaging.GroupMessagesView.as_view(), name="messages.groups.messages"),
    path("messages/groups/<uuid:group_uuid>/read/", views_messaging.GroupReadView.as_view(), name="messages.groups.read"),
    path("messages/groups/<uuid:group_uuid>/members/", views_messaging.GroupMembersView.as_view(), name="messages.groups.members"),
    path("messages/groups/<uuid:group_uuid>/share/pin/", views_messaging.GroupPinShareView.as_view(), name="messages.groups.share.pin"),
    path("messages/<str:peer_slug>/read/", views_messaging.MessageThreadReadView.as_view(), name="messages.read"),
    path("messages/<str:peer_slug>/react/<int:message_id>/", views_messaging.MessageReactionView.as_view(), name="messages.react"),
    path("messages/<str:peer_slug>/messages/<int:message_id>/", views_messaging.MessageDetailView.as_view(), name="messages.detail"),
    path("messages/<str:peer_slug>/", views_messaging.MessageThreadView.as_view(), name="messages.thread"),
    path("safety/checkins/", views.SafetyCheckinsView.as_view(), name="safety.checkins"),
    path("safety/contacts/", views.SafetyContactDefaultsView.as_view(), name="safety.contacts"),
    path("safety/settings/", views.SafetyPreferencesView.as_view(), name="safety.settings"),
    path("safety/checkins/<str:checkin_slug>/check-in/", views.SafetyCheckinMarkSafeView.as_view(), name="safety.checkins.check_in"),
    path("safety/checkins/<str:checkin_slug>/cancel/", views.SafetyCheckinCancelApiView.as_view(), name="safety.checkins.cancel"),
    path("safety/checkins/<str:checkin_slug>/partners/", views.SafetyCheckinPartnersApiView.as_view(), name="safety.checkins.partners"),
    path("safety/checkins/<str:checkin_slug>/partners/<int:partner_id>/", views.SafetyCheckinPartnerDetailApiView.as_view(), name="safety.checkins.partners.detail"),
    path("safety/checkins/<str:checkin_slug>/photos/", views.SafetyCheckinPhotosView.as_view(), name="safety.checkins.photos"),
    path("safety/checkins/<str:checkin_slug>/photos/<int:image_id>/", views.SafetyCheckinPhotoDetailView.as_view(), name="safety.checkins.photos.detail"),
    path("safety/checkins/<str:checkin_slug>/maps/", views.SafetyCheckinMapsView.as_view(), name="safety.checkins.maps"),
    path("safety/checkins/<str:checkin_slug>/maps/<uuid:map_uuid>/", views.SafetyCheckinMapDetailView.as_view(), name="safety.checkins.maps.detail"),
    path("safety/checkins/<str:checkin_slug>/", views.SafetyCheckinDetailApiView.as_view(), name="safety.checkins.detail"),
    # The machine-readable contract (and a browsable view of it) for exactly
    # this surface - internal endpoints are excluded by
    # schema.preprocess_external_api_only. Served without auth: the schema is
    # the published contract, not user data.
    path("schema/", SpectacularAPIView.as_view(authentication_classes=[], permission_classes=[]), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(authentication_classes=[], permission_classes=[], url_name="external_api:schema"), name="docs"),
]

# Concatenation, not include(): see the module docstring for why the flat
# namespace is load-bearing. The modules are listed alphabetically purely so
# merges stay boring - order_by_specificity makes the sequence semantically
# irrelevant, which is the entire point of doing it this way.
urlpatterns: list[URLPattern] = order_by_specificity(
    _CORE_URLPATTERNS
    + urls_assistant.urlpatterns
    + urls_connections.urlpatterns
    + urls_custom_fields.urlpatterns
    + urls_games.urlpatterns
    + urls_labels_extra.urlpatterns
    + urls_lists_extra.urlpatterns
    + urls_memories.urlpatterns
    + urls_messaging.urlpatterns
    + urls_panels.urlpatterns
    + urls_pin_extra.urlpatterns
    + urls_safety_partner.urlpatterns
    + urls_search.urlpatterns
    + urls_site.urlpatterns
    + urls_social.urlpatterns
    + urls_tools.urlpatterns
    + urls_trips.urlpatterns
    + urls_wiki_community.urlpatterns
    + urls_wiki_extra.urlpatterns
)
