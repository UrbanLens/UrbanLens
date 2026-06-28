# Generic imports
from __future__ import annotations

import logging

# Django imports
from django.urls import include, path, re_path
from django.views.generic import TemplateView

# 3rd Party imports
from rest_framework import routers

from urbanlens.dashboard.controllers import (
    aliases,
    badges,
    campus,
    comments,
    detail_pins,
    friendship,
    image_gallery,
    location_wiki,
    maps,
    markup,
    notifications,
    organize,
    pin,
    pin_edit,
    settings,
    setup,
    site_admin,
    tools,
    trip,
    userprofile,
    visits,
)
from urbanlens.dashboard.controllers.index import IndexController
from urbanlens.dashboard.models.badges.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.pin import PinViewSet
from urbanlens.dashboard.models.profile import ProfileViewSet

# from urbanlens.dashboard.models.comments import CommentViewSet
# from urbanlens.dashboard.models.images import ImageViewSet
from urbanlens.dashboard.models.reviews import ReviewViewSet

logger = logging.getLogger(__name__)

app_name = "dashboard"

# Define all our REST API routes
routes = {
    # 'categories': CategoryViewSet,
    "pins": PinViewSet,
    "profiles": ProfileViewSet,
    "reviews": ReviewViewSet,
    # 'comments': CommentViewSet,
    # 'images': ImageViewSet
}
router = routers.DefaultRouter()

# Register each viewset with the router
for route, viewset in routes.items():
    if hasattr(viewset, "basename"):
        router.register(route, viewset, basename=viewset.basename)
    else:
        router.register(route, viewset)

urlpatterns = [
    path(
        "rest/reviews/create_or_update/<int:pk>/",
        ReviewViewSet.as_view({"patch": "create_or_update"}),
        name="review-create-or-update",
    ),
    path("rest/", include(router.urls)),
    re_path("^$", IndexController.as_view(), name="home"),
    path(
        "about/",
        TemplateView.as_view(
            template_name="dashboard/pages/about/index.html",
            extra_context={"page_name": "about"},
        ),
        name="about",
    ),
    path(
        "map/",
        include(
            [
                path("", maps.MapController.as_view({"get": "view_map"}), name="map.view"),
                path("init/", maps.MapController.as_view({"get": "init_map"}), name="map.init"),
                path("pins/", maps.MapController.as_view({"get": "map_pins_json"}), name="map.pins"),
                path("pins/meta/", maps.MapController.as_view({"get": "map_pins_meta"}), name="map.pins.meta"),
                path("pins/<slug:pin_slug>/", maps.MapController.as_view({"get": "map_pin_json"}), name="map.pin.json"),
                path(
                    "campus/",
                    campus.CampusController.as_view({"get": "list_campuses"}),
                    name="campus.list",
                ),
                path(
                    "add/",
                    maps.MapController.as_view({"get": "add_pin", "post": "post_add_pin"}),
                    name="pin.add",
                ),
                path(
                    "edit/<slug:pin_slug>/",
                    maps.MapController.as_view({"get": "get_edit_pin", "post": "edit_pin"}),
                    name="pin.edit",
                ),
                path(
                    "search/",
                    maps.MapController.as_view({"get": "search_map", "post": "search_map_post"}),
                    name="map.search",
                ),
                path(
                    "upload_image/<slug:pin_slug>/",
                    maps.MapController.as_view({"post": "upload_image"}),
                    name="pin.upload_image",
                ),
                # TODO: Assess codebase, but this is probably deprecated since the addition of Badges more generically.
                path(
                    "change_category/<slug:pin_slug>/",
                    maps.MapController.as_view({"post": "change_category"}),
                    name="pin.change_category",
                ),
                # path('delete/<slug:pin_slug>/', MapController.delete_pin, name='delete_pin'),
                # path('add_review/<slug:pin_slug>/', map.MapController.as_view(), name='add_review'),
                path(
                    "pin/",
                    include(
                        [
                            path("<slug:pin_slug>/", pin.PinController.as_view({"get": "view"}), name="pin.details"),
                            path(
                                "<slug:pin_slug>/campus/",
                                campus.CampusController.as_view({"get": "get_campus", "post": "save_campus"}),
                                name="campus.pin",
                            ),
                            path(
                                "<slug:pin_slug>/smithsonian/",
                                pin.PinController.as_view({"get": "get_smithsonian_images"}),
                                name="smithsonian_images",
                            ),
                            path(
                                "<slug:pin_slug>/google/",
                                pin.PinController.as_view({"get": "get_google_images"}),
                                name="google_images",
                            ),
                            path(
                                "<slug:pin_slug>/search/",
                                pin.PinController.as_view({"get": "web_search"}),
                                name="pin.web_search",
                            ),
                            path(
                                "<slug:pin_slug>/satellite_view/",
                                pin.PinController.as_view({"get": "satellite_view_google_image"}),
                                name="pin.satellite_view",
                            ),
                            path(
                                "<slug:pin_slug>/street_view/",
                                pin.PinController.as_view({"get": "street_view"}),
                                name="pin.street_view",
                            ),
                            path(
                                "<slug:pin_slug>/weather/",
                                pin.PinController.as_view({"get": "weather_forecast"}),
                                name="pin.weather_forecast",
                            ),
                            path(
                                "<slug:pin_slug>/visits/",
                                visits.VisitHistoryView.as_view(),
                                name="pin.visits",
                            ),
                            path(
                                "<slug:pin_slug>/visits/<int:visit_id>/delete/",
                                visits.VisitDeleteView.as_view(),
                                name="pin.visit.delete",
                            ),
                            path(
                                "<slug:pin_slug>/detail-pins/",
                                detail_pins.DetailPinPanelView.as_view(),
                                name="pin.detail_pins",
                            ),
                            path(
                                "<slug:pin_slug>/detail-pins/json/",
                                detail_pins.DetailPinJsonView.as_view(),
                                name="pin.detail_pins.json",
                            ),
                            path(
                                "<slug:pin_slug>/detail-pins/<uuid:detail_pin_uuid>/",
                                detail_pins.DetailPinEditView.as_view(),
                                name="pin.detail_pin.edit",
                            ),
                            path(
                                "<slug:pin_slug>/markup/json/",
                                markup.MarkupJsonView.as_view(),
                                name="pin.markup.json",
                            ),
                            path(
                                "<slug:pin_slug>/markup/",
                                markup.MarkupView.as_view(),
                                name="pin.markup",
                            ),
                            path(
                                "<slug:pin_slug>/markup/<uuid:markup_uuid>/",
                                markup.MarkupEditView.as_view(),
                                name="pin.markup.edit",
                            ),
                            path(
                                "<slug:pin_slug>/overview/",
                                pin_edit.PinOverviewView.as_view(),
                                name="pin.overview",
                            ),
                            path(
                                "<slug:pin_slug>/edit/",
                                pin_edit.PinEditView.as_view(),
                                name="pin.edit",
                            ),
                            path(
                                "<slug:pin_slug>/notes/",
                                pin_edit.PinNotesView.as_view(),
                                name="pin.notes",
                            ),
                            path(
                                "<slug:pin_slug>/notes/<int:note_id>/delete/",
                                pin_edit.PinNoteDeleteView.as_view(),
                                name="pin.note.delete",
                            ),
                            path(
                                "<slug:pin_slug>/link/",
                                pin_edit.PinRelinkView.as_view(),
                                name="pin.link",
                            ),
                            path(
                                "<slug:pin_slug>/link/<slug:location_slug>/",
                                pin_edit.PinRelinkView.as_view(),
                                name="pin.link.to",
                            ),
                            path(
                                "<slug:pin_slug>/aliases/",
                                aliases.PinAliasView.as_view(),
                                name="pin.aliases",
                            ),
                            path(
                                "<slug:pin_slug>/aliases/<int:alias_id>/delete/",
                                aliases.PinAliasDeleteView.as_view(),
                                name="pin.alias.delete",
                            ),
                            path(
                                "<slug:pin_slug>/comments/",
                                comments.PinCommentsView.as_view(),
                                name="pin.comments",
                            ),
                            path(
                                "<slug:pin_slug>/comments/<int:comment_id>/delete/",
                                comments.PinCommentDeleteView.as_view(),
                                name="pin.comment.delete",
                            ),
                            path(
                                "<slug:pin_slug>/wikipedia/",
                                pin.PinController.as_view({"get": "wikipedia_info"}),
                                name="pin.wikipedia",
                            ),
                            path(
                                "<slug:pin_slug>/wikimedia/",
                                pin.PinController.as_view({"get": "wikimedia_assets"}),
                                name="pin.wikimedia",
                            ),
                            path(
                                "<slug:pin_slug>/loopnet/",
                                pin.PinController.as_view({"get": "loopnet_info"}),
                                name="pin.loopnet",
                            ),
                            path(
                                "<slug:pin_slug>/nps/",
                                pin.PinController.as_view({"get": "nps_info"}),
                                name="pin.nps",
                            ),
                            path(
                                "<slug:pin_slug>/gallery/",
                                image_gallery.PinGalleryView.as_view(),
                                name="pin.gallery",
                            ),
                            path(
                                "<slug:pin_slug>/gallery/json/",
                                image_gallery.PinGalleryJsonView.as_view(),
                                name="pin.gallery.json",
                            ),
                            path(
                                "<slug:pin_slug>/gallery/<int:image_id>/",
                                image_gallery.PinImageView.as_view(),
                                name="pin.gallery.image",
                            ),
                            # No-trailing-slash variant so DELETE/POST fetch calls work even
                            # when APPEND_SLASH would otherwise downgrade the method to GET.
                            path(
                                "<slug:pin_slug>/gallery/<int:image_id>",
                                image_gallery.PinImageView.as_view(),
                            ),
                            path(
                                "import/",
                                include(
                                    [
                                        path(
                                            "form/",
                                            pin.PinController.as_view({"get": "import_form"}),
                                            name="pin.import.form",
                                        ),
                                        path(
                                            "upload/",
                                            pin.PinController.as_view({"post": "upload_takeout"}),
                                            name="pin.upload.takeout",
                                        ),
                                        path(
                                            "preview/",
                                            pin.PinController.as_view({"post": "parse_for_preview"}),
                                            name="pin.import.preview",
                                        ),
                                        path(
                                            "confirmed/",
                                            pin.PinController.as_view({"post": "import_confirmed"}),
                                            name="pin.import.confirmed",
                                        ),
                                    ],
                                ),
                            ),
                        ],
                    ),
                ),
            ],
        ),
    ),
    path(
        "profile/",
        include(
            [
                path("", userprofile.ViewProfileView.as_view(), name="profile.view"),
                path("edit/", userprofile.EditProfileView.as_view(), name="profile.edit"),
                path("edit/field/", userprofile.ProfileFieldUpdateView.as_view(), name="profile.field.update"),
                path("edit/social/verify/", userprofile.SocialLinkVerifyView.as_view(), name="profile.social.verify"),
                path("<slug:profile_slug>/", userprofile.ViewProfileView.as_view(), name="profile.view_user"),
                path("<slug:profile_slug>/note/", userprofile.ProfileNoteView.as_view(), name="profile.note"),
                path(
                    "<slug:profile_slug>/note/<int:note_id>/delete/",
                    userprofile.ProfileNoteDeleteView.as_view(),
                    name="profile.note.delete",
                ),
                path(
                    "<slug:profile_slug>/note/<int:note_id>/edit/",
                    userprofile.ProfileNoteEditView.as_view(),
                    name="profile.note.edit",
                ),
                path(
                    "<slug:profile_slug>/badge/<int:badge_id>/",
                    userprofile.ProfileBadgeToggleView.as_view(),
                    name="profile.badge_toggle",
                ),
                path(
                    "<slug:profile_slug>/trust/",
                    userprofile.ProfileTrustView.as_view(),
                    name="profile.trust",
                ),
            ],
        ),
    ),
    path("settings/", settings.SettingsView.as_view(), name="settings.view"),
    path("settings/geocode/", settings.geocode_address, name="settings.geocode"),
    path("settings/map-position/", settings.SaveMapPositionView.as_view(), name="settings.save_map_position"),
    path("settings/map-dark-mode/", settings.SaveMapDarkModeView.as_view(), name="settings.save_map_dark_mode"),
    re_path(
        r"^(?P<badge_kind>tags?|categor(y|ies)|status(es)?|people)/",
        include([
            path("", badges.BadgeKindIndexView.as_view(), name="badge.index"),
            path("create/", badges.BadgeCreateView.as_view(), name="badge.create"),
            path("rows/", badges.BadgeRowsView.as_view(), name="badge.rows"),
            path("<int:badge_id>/edit/", badges.BadgeEditView.as_view(), name="badge.edit"),
            path("<int:badge_id>/delete/", badges.BadgeDeleteView.as_view(), name="badge.delete"),
            path("<int:badge_id>/merge/", badges.BadgeMergeView.as_view(), name="badge.merge"),
            path("<int:badge_id>/customize/", badges.BadgeCustomizeView.as_view(), name="badge.customize"),
            path("reorder/", badges.BadgeReorderView.as_view(), name="badge.reorder"),
            path("bulk-delete/", badges.BadgeBulkDeleteView.as_view(), name="badge.bulk_delete"),
            path("bulk-edit/", badges.BadgeBulkEditView.as_view(), name="badge.bulk_edit"),
            path(
                "bulk-convert/",
                badges.BadgeBulkConvertView.as_view(),
                name="badge.bulk_convert",
            ),
            path(
                "bulk-convert-status/",
                badges.BadgeBulkConvertView.as_view(target_kind=KIND_STATUS),
                name="badge.bulk_convert_status",
            ),
            path(
                "bulk-convert-tag/",
                badges.BadgeBulkConvertView.as_view(target_kind=KIND_TAG),
                name="badge.bulk_convert_tag",
            ),
            path(
                "bulk-convert-category/",
                badges.BadgeBulkConvertView.as_view(target_kind=KIND_CATEGORY),
                name="badge.bulk_convert_category",
            ),
            path("multi-merge/", badges.BadgeMultiMergeView.as_view(), name="badge.multi_merge"),
            path("pin/<slug:pin_slug>/", badges.BadgePinMembershipView.as_view(), name="badge.pin"),
            path(
                "location/<slug:location_slug>/",
                badges.BadgeLocationMembershipView.as_view(),
                name="badge.location",
            ),
        ]),
    ),
    path(
        "friendship/",
        include(
            [
                path(
                    "list/<int:profile_id>",
                    friendship.FriendController.as_view({"get": "friend_list"}),
                    name="friend.list",
                ),
                path(
                    "friends/<int:profile_id>/",
                    friendship.FriendController.as_view({"get": "friends_page"}),
                    name="friend.page",
                ),
                path(
                    "request/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "request_friend"}),
                    name="friend.request",
                ),
                path(
                    "accept/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "accept_friend"}),
                    name="friend.accept",
                ),
                path(
                    "reject/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "reject_friend"}),
                    name="friend.reject",
                ),
                path(
                    "ignore/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "ignore_friend"}),
                    name="friend.ignore",
                ),
                path(
                    "remove/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "remove_friend"}),
                    name="friend.remove",
                ),
                path(
                    "block/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "block_friend"}),
                    name="friend.block",
                ),
                path(
                    "mute/<int:profile_id>",
                    friendship.FriendController.as_view({"post": "mute_friend"}),
                    name="friend.mute",
                ),
                path(
                    "respond/<int:from_profile_id>/",
                    friendship.FriendController.as_view({"post": "friend_request_respond"}),
                    name="friend.respond",
                ),
                path(
                    "invite/",
                    friendship.FriendController.as_view({"post": "invite_by_email"}),
                    name="friend.invite_email",
                ),
            ],
        ),
    ),
    path(
        "location/",
        include(
            [
                path(
                    "<slug:location_slug>/wiki/",
                    location_wiki.LocationWikiView.as_view(),
                    name="location.wiki",
                ),
                path(
                    "<slug:location_slug>/wiki/edit/",
                    location_wiki.LocationWikiEditView.as_view(),
                    name="location.wiki.edit",
                ),
                path(
                    "<slug:location_slug>/wiki/bbox/",
                    location_wiki.LocationWikiBboxView.as_view(),
                    name="location.wiki.bbox",
                ),
                path(
                    "<slug:location_slug>/wiki/history/",
                    location_wiki.LocationWikiHistoryView.as_view(),
                    name="location.wiki.history",
                ),
                path(
                    "<slug:location_slug>/wiki/history/<int:edit_id>/revert/",
                    location_wiki.LocationWikiRevertView.as_view(),
                    name="location.wiki.revert",
                ),
                path(
                    "<slug:location_slug>/wiki/detail-pins/json/",
                    detail_pins.LocationDetailPinJsonView.as_view(),
                    name="location.wiki.detail_pins.json",
                ),
                path(
                    "<slug:location_slug>/wiki/detail-pins/panel/",
                    detail_pins.LocationWikiDetailPinView.as_view(),
                    name="location.wiki.detail_pins.panel",
                ),
                path(
                    "<slug:location_slug>/wiki/detail-pins/<uuid:detail_pin_uuid>/delete/",
                    detail_pins.LocationWikiDetailPinDeleteView.as_view(),
                    name="location.wiki.detail_pin.delete",
                ),
                path(
                    "<slug:location_slug>/wiki/detail-pins/<uuid:detail_pin_uuid>/edit/",
                    detail_pins.LocationWikiDetailPinEditView.as_view(),
                    name="location.wiki.detail_pin.edit",
                ),
                path(
                    "<slug:location_slug>/wiki/comments/",
                    comments.WikiCommentsView.as_view(),
                    name="location.wiki.comments",
                ),
                path(
                    "<slug:location_slug>/wiki/comments/<int:comment_id>/delete/",
                    comments.WikiCommentDeleteView.as_view(),
                    name="location.wiki.comment.delete",
                ),
                path(
                    "<slug:location_slug>/wiki/aliases/",
                    aliases.LocationAliasView.as_view(),
                    name="location.wiki.aliases",
                ),
                path(
                    "<slug:location_slug>/wiki/aliases/<int:alias_id>/delete/",
                    aliases.LocationAliasDeleteView.as_view(),
                    name="location.wiki.alias.delete",
                ),
                path(
                    "<slug:location_slug>/wiki/gallery/",
                    image_gallery.WikiGalleryView.as_view(),
                    name="location.wiki.gallery",
                ),
                path(
                    "<slug:location_slug>/wiki/gallery/json/",
                    image_gallery.WikiGalleryJsonView.as_view(),
                    name="location.wiki.gallery.json",
                ),
                path(
                    "<slug:location_slug>/wiki/gallery/<int:image_id>/",
                    image_gallery.WikiImageView.as_view(),
                    name="location.wiki.gallery.image",
                ),
            ],
        ),
    ),
    path(
        "trips/",
        include(
            [
                path("", trip.TripListView.as_view(), name="trips.list"),
                path("create/", trip.TripCreateView.as_view(), name="trips.create"),
                path("location-search/", trip.TripLocationSearchView.as_view(), name="trips.location_search"),
                path("<uuid:trip_uuid>/", trip.TripDetailView.as_view(), name="trips.detail"),
                path("<uuid:trip_uuid>/edit/", trip.TripEditView.as_view(), name="trips.edit"),
                path("<uuid:trip_uuid>/delete/", trip.TripDeleteView.as_view(), name="trips.delete"),
                path("<uuid:trip_uuid>/activities/", trip.TripActivitiesView.as_view(), name="trips.activities"),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/delete/",
                    trip.TripActivityDeleteView.as_view(),
                    name="trips.activity.delete",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/edit/",
                    trip.TripActivityEditView.as_view(),
                    name="trips.activity.edit",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/status/",
                    trip.TripActivityStatusView.as_view(),
                    name="trips.activity.status",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/move/",
                    trip.TripActivityMoveView.as_view(),
                    name="trips.activity.move",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/position/",
                    trip.TripActivityPositionView.as_view(),
                    name="trips.activity.position",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/position",
                    trip.TripActivityPositionView.as_view(),
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/vote/",
                    trip.TripActivityVoteView.as_view(),
                    name="trips.activity.vote",
                ),
                path(
                    "<uuid:trip_uuid>/activities/<int:activity_id>/complete/",
                    trip.TripActivityCompleteView.as_view(),
                    name="trips.activity.complete",
                ),
                path(
                    "<uuid:trip_uuid>/child-trip-search/",
                    trip.TripChildTripSearchView.as_view(),
                    name="trips.child_trip_search",
                ),
                path("<uuid:trip_uuid>/comments/", trip.TripCommentsView.as_view(), name="trips.comments"),
                path(
                    "<uuid:trip_uuid>/comments/<int:comment_id>/delete/",
                    trip.TripCommentDeleteView.as_view(),
                    name="trips.comment.delete",
                ),
                path(
                    "<uuid:trip_uuid>/comments/<int:comment_id>/react/",
                    comments.TripCommentReactionView.as_view(),
                    name="trips.comment.react",
                ),
                path("<uuid:trip_uuid>/members/", trip.TripMembersView.as_view(), name="trips.members"),
                path("<uuid:trip_uuid>/members", trip.TripMembersView.as_view()),
                path(
                    "<uuid:trip_uuid>/members/<int:profile_id>/remove/",
                    trip.TripMemberRemoveView.as_view(),
                    name="trips.member.remove",
                ),
                path(
                    "<uuid:trip_uuid>/members/<int:profile_id>/organizer/",
                    trip.TripMemberOrganizerView.as_view(),
                    name="trips.member.organizer",
                ),
                path("<uuid:trip_uuid>/rsvp/", trip.TripMemberRSVPView.as_view(), name="trips.rsvp"),
                path("<uuid:trip_uuid>/rsvp", trip.TripMemberRSVPView.as_view()),
                path("<uuid:trip_uuid>/leave/", trip.TripLeaveView.as_view(), name="trips.leave"),
                path("<uuid:trip_uuid>/leave", trip.TripLeaveView.as_view()),
                path("<uuid:trip_uuid>/settings/", trip.TripSettingsView.as_view(), name="trips.settings"),
                path("<uuid:trip_uuid>/settings", trip.TripSettingsView.as_view()),
                path("<uuid:trip_uuid>/map-data/", trip.TripMapDataView.as_view(), name="trips.map_data"),
                path("<uuid:trip_uuid>/weather/", trip.TripWeatherView.as_view(), name="trips.weather"),
            ],
        ),
    ),
    path(
        "organize/",
        include(
            [
                path("", organize.OrganizeIndexView.as_view(), name="organize.index"),
                path("priority/save/", organize.OrganizePrioritySaveView.as_view(), name="organize.priority.save"),
            ],
        ),
    ),
    path(
        "comments/",
        include(
            [
                path("<int:comment_id>/react/", comments.CommentReactionView.as_view(), name="comment.react"),
                path("locations/", comments.PinnedLocationsJsonView.as_view(), name="comment.locations"),
                path("map-pins/", comments.CommentMapPinsView.as_view(), name="comment.map_pins"),
            ],
        ),
    ),
    path(
        "notifications/",
        include(
            [
                path("dropdown/", notifications.NotificationDropdownView.as_view(), name="notifications.dropdown"),
                path("read-all/", notifications.NotificationMarkAllReadView.as_view(), name="notifications.read_all"),
                path(
                    "unread-count/",
                    notifications.NotificationUnreadCountView.as_view(),
                    name="notifications.unread_count",
                ),
                path(
                    "preferences/",
                    notifications.NotificationPreferencesView.as_view(),
                    name="notifications.preferences",
                ),
                path(
                    "<int:notification_id>/read/",
                    notifications.NotificationMarkReadView.as_view(),
                    name="notifications.read",
                ),
            ],
        ),
    ),
    path(
        "tools/",
        include(
            [
                path("", tools.ToolsIndexView.as_view(), name="tools.index"),
                path("export/start/", tools.ExportStartView.as_view(), name="tools.export.start"),
                path("export/status/<str:job_id>/", tools.ExportStatusView.as_view(), name="tools.export.status"),
                path("export/download/<str:job_id>/", tools.ExportDownloadView.as_view(), name="tools.export.download"),
                path("import/start/", tools.ImportStartView.as_view(), name="tools.import.start"),
                path("import/status/<str:job_id>/", tools.ImportStatusView.as_view(), name="tools.import.status"),
                path("backup/start/", tools.BackupStartView.as_view(), name="tools.backup.start"),
            ],
        ),
    ),
    path("setup/", setup.SetupWizardView.as_view(), name="setup"),
    path("tasks/<str:task_id>/status/", site_admin.CeleryTaskStatusView.as_view(), name="celery_task_status"),
    path("site-admin/", site_admin.SiteAdminView.as_view(), name="site_admin"),
    path("site-admin/stats/", site_admin.SiteAdminStatsView.as_view(), name="site_admin_stats"),
    path("site-admin/stats/pull-latest-code/", site_admin.SiteAdminPullLatestCodeView.as_view(), name="site_admin_pull_latest_code"),
    path("site-admin/subscriptions/", site_admin.SiteAdminSubscriptionsView.as_view(), name="site_admin_subscriptions"),
    path(
        "site-admin/ui-components/",
        site_admin.SiteAdminUIComponentsView.as_view(),
        name="site_admin_ui_components",
    ),
    path(
        "site-admin/dev/toggle-theme/",
        site_admin.DevToolbarToggleThemeView.as_view(),
        name="dev_toolbar.toggle_theme",
    ),
    path(
        "site-admin/dev/toggle-map-dark-mode/",
        site_admin.DevToolbarToggleMapDarkModeView.as_view(),
        name="dev_toolbar.toggle_map_dark_mode",
    ),
    path(
        "site-admin/dev/clear-session/",
        site_admin.DevToolbarClearSessionView.as_view(),
        name="dev_toolbar.clear_session",
    ),
    path(
        "site-admin/dev/reset-onboarding/",
        site_admin.DevToolbarResetOnboardingView.as_view(),
        name="dev_toolbar.reset_onboarding",
    ),
    path("test_ai/", pin.PinController.as_view({"get": "test_ai"}), name="test_ai"),
    path("", include("social_django.urls", namespace="social")),
    re_path(".*", TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name="404"),
]
