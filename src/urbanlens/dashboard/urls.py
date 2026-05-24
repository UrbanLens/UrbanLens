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

from urbanlens.dashboard.controllers import campus, detail_pins, friendship, location_wiki, maps, pin, settings, tags, userprofile, visits
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
            ],
        ),
    ),
    path("test_ai/", pin.PinController.as_view({"get": "test_ai"}), name="test_ai"),
    path("", include("social_django.urls", namespace="social")),
    re_path(".*", TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name="404"),
]
