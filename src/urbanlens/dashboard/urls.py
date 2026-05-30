"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    urls.py                                                                                              *
*        Path:    /dashboard/urls.py                                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations

import logging

# Django imports
from django.urls import include, path, re_path
from django.views.generic import TemplateView

# 3rd Party imports
from rest_framework import routers

from urbanlens.dashboard.controllers import aliases, campus, categories, comments, detail_pins, friendship, location_wiki, maps, markup, notifications, organize, pin, pin_edit, settings, site_admin, tags, trip, userprofile, visits
from urbanlens.dashboard.controllers.index import IndexController

# from urbanlens.dashboard.models.categories import CategoryViewSet
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
        "map/",
        include(
            [
                path("", maps.MapController.as_view({"get": "view_map"}), name="map.view"),
                path("init/", maps.MapController.as_view({"get": "init_map"}), name="map.init"),
                path("pins/", maps.MapController.as_view({"get": "map_pins_json"}), name="map.pins"),
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
                    "edit/<uuid:pin_uuid>/",
                    maps.MapController.as_view({"get": "get_edit_pin", "post": "edit_pin"}),
                    name="pin.edit",
                ),
                path(
                    "search/",
                    maps.MapController.as_view({"get": "search_map", "post": "search_map_post"}),
                    name="map.search",
                ),
                path(
                    "upload_image/<uuid:pin_uuid>/",
                    maps.MapController.as_view({"post": "upload_image"}),
                    name="pin.upload_image",
                ),
                path(
                    "change_category/<uuid:pin_uuid>/",
                    maps.MapController.as_view({"post": "change_category"}),
                    name="pin.change_category",
                ),
                # path('delete/<uuid:pin_uuid>/', MapController.delete_pin, name='delete_pin'),
                # path('add_review/<uuid:pin_uuid>/', map.MapController.as_view(), name='add_review'),
                path(
                    "pin/",
                    include(
                        [
                            path("<uuid:pin_uuid>/", pin.PinController.as_view({"get": "view"}), name="pin.details"),
                            path(
                                "<uuid:pin_uuid>/campus/",
                                campus.CampusController.as_view({"get": "get_campus", "post": "save_campus"}),
                                name="campus.pin",
                            ),
                            path(
                                "<uuid:pin_uuid>/smithsonian/",
                                pin.PinController.as_view({"get": "get_smithsonian_images"}),
                                name="smithsonian_images",
                            ),
                            path(
                                "<uuid:pin_uuid>/google/",
                                pin.PinController.as_view({"get": "get_google_images"}),
                                name="google_images",
                            ),
                            path(
                                "<uuid:pin_uuid>/search/",
                                pin.PinController.as_view({"get": "web_search"}),
                                name="pin.web_search",
                            ),
                            path(
                                "<uuid:pin_uuid>/satellite_view/",
                                pin.PinController.as_view({"get": "satellite_view_google_image"}),
                                name="pin.satellite_view",
                            ),
                            path(
                                "<uuid:pin_uuid>/street_view/",
                                pin.PinController.as_view({"get": "street_view"}),
                                name="pin.street_view",
                            ),
                            path(
                                "<uuid:pin_uuid>/weather/",
                                pin.PinController.as_view({"get": "weather_forecast"}),
                                name="pin.weather_forecast",
                            ),
                            path(
                                "<uuid:pin_uuid>/visits/",
                                visits.VisitHistoryView.as_view(),
                                name="pin.visits",
                            ),
                            path(
                                "<uuid:pin_uuid>/visits/<int:visit_id>/delete/",
                                visits.VisitDeleteView.as_view(),
                                name="pin.visit.delete",
                            ),
                            path(
                                "<uuid:pin_uuid>/detail-pins/",
                                detail_pins.DetailPinPanelView.as_view(),
                                name="pin.detail_pins",
                            ),
                            path(
                                "<uuid:pin_uuid>/detail-pins/json/",
                                detail_pins.DetailPinJsonView.as_view(),
                                name="pin.detail_pins.json",
                            ),
                            path(
                                "<uuid:pin_uuid>/detail-pins/<uuid:detail_pin_uuid>/",
                                detail_pins.DetailPinEditView.as_view(),
                                name="pin.detail_pin.edit",
                            ),
                            path(
                                "<uuid:pin_uuid>/markup/json/",
                                markup.MarkupJsonView.as_view(),
                                name="pin.markup.json",
                            ),
                            path(
                                "<uuid:pin_uuid>/markup/",
                                markup.MarkupView.as_view(),
                                name="pin.markup",
                            ),
                            path(
                                "<uuid:pin_uuid>/markup/<uuid:markup_uuid>/",
                                markup.MarkupEditView.as_view(),
                                name="pin.markup.edit",
                            ),
                            path(
                                "<uuid:pin_uuid>/overview/",
                                pin_edit.PinOverviewView.as_view(),
                                name="pin.overview",
                            ),
                            path(
                                "<uuid:pin_uuid>/edit/",
                                pin_edit.PinEditView.as_view(),
                                name="pin.edit",
                            ),
                            path(
                                "<uuid:pin_uuid>/notes/",
                                pin_edit.PinNotesView.as_view(),
                                name="pin.notes",
                            ),
                            path(
                                "<uuid:pin_uuid>/notes/<int:note_id>/delete/",
                                pin_edit.PinNoteDeleteView.as_view(),
                                name="pin.note.delete",
                            ),
                            path(
                                "<uuid:pin_uuid>/aliases/",
                                aliases.PinAliasView.as_view(),
                                name="pin.aliases",
                            ),
                            path(
                                "<uuid:pin_uuid>/aliases/<int:alias_id>/delete/",
                                aliases.PinAliasDeleteView.as_view(),
                                name="pin.alias.delete",
                            ),
                            path(
                                "<uuid:pin_uuid>/comments/",
                                comments.PinCommentsView.as_view(),
                                name="pin.comments",
                            ),
                            path(
                                "<uuid:pin_uuid>/comments/<int:comment_id>/delete/",
                                comments.PinCommentDeleteView.as_view(),
                                name="pin.comment.delete",
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
                path("<int:profile_id>/", userprofile.ViewProfileView.as_view(), name="profile.view_user"),
            ],
        ),
    ),
    path("settings/", settings.SettingsView.as_view(), name="settings.view"),
    path(
        "tags/",
        include(
            [
                path("", tags.TagIndexView.as_view(), name="tag.index"),
                path("create/", tags.TagCreateView.as_view(), name="tag.create"),
                path("<int:tag_id>/edit/", tags.TagEditView.as_view(), name="tag.edit"),
                path("<int:tag_id>/delete/", tags.TagDeleteView.as_view(), name="tag.delete"),
                path("<int:tag_id>/merge/", tags.TagMergeView.as_view(), name="tag.merge"),
                path("rows/", tags.TagRowsView.as_view(), name="tag.rows"),
                path("reorder/", tags.TagReorderView.as_view(), name="tag.reorder"),
                path("bulk-delete/", tags.TagBulkDeleteView.as_view(), name="tag.bulk_delete"),
                path("bulk-edit/", tags.TagBulkEditView.as_view(), name="tag.bulk_edit"),
                path("bulk-convert/", tags.TagBulkConvertView.as_view(), name="tag.bulk_convert"),
                path("multi-merge/", tags.TagMultiMergeView.as_view(), name="tag.multi_merge"),
                path("<int:tag_id>/customize/", tags.TagCustomizeView.as_view(), name="tag.customize"),
                path("pin/<uuid:pin_uuid>/", tags.TagMembershipView.as_view(), name="tag.membership"),
            ],
        ),
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
            ],
        ),
    ),
    path(
        "location/",
        include(
            [
                path(
                    "<uuid:location_uuid>/wiki/",
                    location_wiki.LocationWikiView.as_view(),
                    name="location.wiki",
                ),
                path(
                    "<uuid:location_uuid>/wiki/edit/",
                    location_wiki.LocationWikiEditView.as_view(),
                    name="location.wiki.edit",
                ),
                path(
                    "<uuid:location_uuid>/wiki/bbox/",
                    location_wiki.LocationWikiBboxView.as_view(),
                    name="location.wiki.bbox",
                ),
                path(
                    "<uuid:location_uuid>/wiki/history/",
                    location_wiki.LocationWikiHistoryView.as_view(),
                    name="location.wiki.history",
                ),
                path(
                    "<uuid:location_uuid>/wiki/history/<int:edit_id>/revert/",
                    location_wiki.LocationWikiRevertView.as_view(),
                    name="location.wiki.revert",
                ),
                path(
                    "<uuid:location_uuid>/wiki/detail-pins/json/",
                    detail_pins.LocationDetailPinJsonView.as_view(),
                    name="location.wiki.detail_pins.json",
                ),
                path(
                    "<uuid:location_uuid>/wiki/detail-pins/panel/",
                    detail_pins.LocationWikiDetailPinView.as_view(),
                    name="location.wiki.detail_pins.panel",
                ),
                path(
                    "<uuid:location_uuid>/wiki/detail-pins/<uuid:detail_pin_uuid>/delete/",
                    detail_pins.LocationWikiDetailPinDeleteView.as_view(),
                    name="location.wiki.detail_pin.delete",
                ),
                path(
                    "<uuid:location_uuid>/wiki/detail-pins/<uuid:detail_pin_uuid>/edit/",
                    detail_pins.LocationWikiDetailPinEditView.as_view(),
                    name="location.wiki.detail_pin.edit",
                ),
                path(
                    "<uuid:location_uuid>/wiki/comments/",
                    comments.WikiCommentsView.as_view(),
                    name="location.wiki.comments",
                ),
                path(
                    "<uuid:location_uuid>/wiki/comments/<int:comment_id>/delete/",
                    comments.WikiCommentDeleteView.as_view(),
                    name="location.wiki.comment.delete",
                ),
                path(
                    "<uuid:location_uuid>/wiki/aliases/",
                    aliases.LocationAliasView.as_view(),
                    name="location.wiki.aliases",
                ),
                path(
                    "<uuid:location_uuid>/wiki/aliases/<int:alias_id>/delete/",
                    aliases.LocationAliasDeleteView.as_view(),
                    name="location.wiki.alias.delete",
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
                path("<uuid:trip_uuid>/activities/<int:activity_id>/delete/", trip.TripActivityDeleteView.as_view(), name="trips.activity.delete"),
                path("<uuid:trip_uuid>/activities/<int:activity_id>/edit/", trip.TripActivityEditView.as_view(), name="trips.activity.edit"),
                path("<uuid:trip_uuid>/activities/<int:activity_id>/status/", trip.TripActivityStatusView.as_view(), name="trips.activity.status"),
                path("<uuid:trip_uuid>/activities/<int:activity_id>/move/", trip.TripActivityMoveView.as_view(), name="trips.activity.move"),
                path("<uuid:trip_uuid>/activities/<int:activity_id>/position/", trip.TripActivityPositionView.as_view(), name="trips.activity.position"),
                path("<uuid:trip_uuid>/activities/<int:activity_id>/vote/", trip.TripActivityVoteView.as_view(), name="trips.activity.vote"),
                path("<uuid:trip_uuid>/child-trip-search/", trip.TripChildTripSearchView.as_view(), name="trips.child_trip_search"),
                path("<uuid:trip_uuid>/comments/", trip.TripCommentsView.as_view(), name="trips.comments"),
                path("<uuid:trip_uuid>/comments/<int:comment_id>/delete/", trip.TripCommentDeleteView.as_view(), name="trips.comment.delete"),
                path("<uuid:trip_uuid>/comments/<int:comment_id>/react/", comments.TripCommentReactionView.as_view(), name="trips.comment.react"),
                path("<uuid:trip_uuid>/members/", trip.TripMembersView.as_view(), name="trips.members"),
                path("<uuid:trip_uuid>/members/<int:profile_id>/remove/", trip.TripMemberRemoveView.as_view(), name="trips.member.remove"),
                path("<uuid:trip_uuid>/rsvp/", trip.TripMemberRSVPView.as_view(), name="trips.rsvp"),
                path("<uuid:trip_uuid>/leave/", trip.TripLeaveView.as_view(), name="trips.leave"),
                path("<uuid:trip_uuid>/settings/", trip.TripSettingsView.as_view(), name="trips.settings"),
                path("<uuid:trip_uuid>/map-data/", trip.TripMapDataView.as_view(), name="trips.map_data"),
                path("<uuid:trip_uuid>/weather/", trip.TripWeatherView.as_view(), name="trips.weather"),
            ],
        ),
    ),
    path(
        "categories/",
        include(
            [
                path("", categories.CategoryIndexView.as_view(), name="category.index"),
                path("create/", categories.CategoryCreateView.as_view(), name="category.create"),
                path("<int:cat_id>/edit/", categories.CategoryEditView.as_view(), name="category.edit"),
                path("<int:cat_id>/delete/", categories.CategoryDeleteView.as_view(), name="category.delete"),
                path("<int:cat_id>/merge/", categories.CategoryMergeView.as_view(), name="category.merge"),
                path("merge/", categories.CategoryMultiMergeView.as_view(), name="category.multi_merge"),
                path("bulk-delete/", categories.CategoryBulkDeleteView.as_view(), name="category.bulk_delete"),
                path("bulk-edit/", categories.CategoryBulkEditView.as_view(), name="category.bulk_edit"),
                path("bulk-convert/", categories.CategoryBulkConvertView.as_view(), name="category.bulk_convert"),
                path("rows/", categories.CategoryRowsView.as_view(), name="category.rows"),
                path("<int:cat_id>/customize/", categories.CategoryCustomizeView.as_view(), name="category.customize"),
                path("reorder/", categories.CategoryReorderView.as_view(), name="category.reorder"),
                path("pin/<uuid:pin_uuid>/", categories.CategoryPinMembershipView.as_view(), name="category.pin"),
                path("location/<uuid:location_uuid>/", categories.CategoryLocationMembershipView.as_view(), name="category.location"),
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
            ],
        ),
    ),
    path(
        "notifications/",
        include(
            [
                path("dropdown/", notifications.NotificationDropdownView.as_view(), name="notifications.dropdown"),
                path("read-all/", notifications.NotificationMarkAllReadView.as_view(), name="notifications.read_all"),
                path("unread-count/", notifications.NotificationUnreadCountView.as_view(), name="notifications.unread_count"),
                path("preferences/", notifications.NotificationPreferencesView.as_view(), name="notifications.preferences"),
                path("<int:notification_id>/read/", notifications.NotificationMarkReadView.as_view(), name="notifications.read"),
            ],
        ),
    ),
    path("site-admin/", site_admin.SiteAdminView.as_view(), name="site_admin"),
    path("test_ai/", pin.PinController.as_view({"get": "test_ai"}), name="test_ai"),
    path("", include("social_django.urls", namespace="social")),
    re_path(".*", TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name="404"),
]
