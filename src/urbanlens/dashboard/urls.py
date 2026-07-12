# Generic imports
from __future__ import annotations

import logging

# Django imports
from django.urls import include, path, re_path
from django.views.generic import TemplateView

# 3rd Party imports
from rest_framework import routers

from urbanlens.dashboard.controllers import (
    account_deletion,
    aliases,
    badges,
    boundary,
    calendar_sync,
    comments,
    custom_fields,
    detail_pins,
    direct_message_shares,
    direct_messages,
    e2ee,
    flickr,
    friendship,
    google_photos,
    image_gallery,
    immich,
    location_wiki,
    map_sharing,
    maps,
    markup,
    media_proxy,
    memories,
    notifications,
    onboarding,
    organize,
    photos,
    pin,
    pin_bulk,
    pin_edit,
    pin_lists,
    pin_sharing,
    safety,
    saved_filters,
    settings,
    setup,
    site_admin,
    thanks,
    tools,
    trip,
    undo,
    userprofile,
    visit_suggestions,
    visits,
    wiki_create,
)
from urbanlens.dashboard.controllers.index import IndexController
from urbanlens.dashboard.models.badges.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.pin import PinViewSet
from urbanlens.dashboard.models.reviews import ReviewViewSet

logger = logging.getLogger(__name__)

app_name = "dashboard"

# The REST surface is deliberately minimal: the frontend only uses
# PATCH/DELETE on individual pins (map popup quick-edit, pin move, delete)
# and the review-create-or-update path below (star-rating widget). Nothing
# external consumes this API; expose more only when the app itself needs it.
router = routers.DefaultRouter()
router.register("pins", PinViewSet, basename=PinViewSet.basename)

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
        "values/",
        TemplateView.as_view(
            template_name="dashboard/pages/values/index.html",
            extra_context={"page_name": "values"},
        ),
        name="values",
    ),
    path(
        "faq/",
        TemplateView.as_view(
            template_name="dashboard/pages/faq/index.html",
            extra_context={"page_name": "faq"},
        ),
        name="faq",
    ),
    path(
        "terms/",
        TemplateView.as_view(
            template_name="dashboard/pages/legal/terms.html",
            extra_context={"page_name": "terms"},
        ),
        name="terms",
    ),
    path("thanks/", thanks.ThanksView.as_view(), name="thanks"),
    path(
        "help/import-pins/",
        TemplateView.as_view(
            template_name="dashboard/pages/help/import_pins.html",
            extra_context={"page_name": "help_import_pins"},
        ),
        name="help.import_pins",
    ),
    path(
        "map/",
        include(
            [
                path("", maps.MapController.as_view({"get": "view_map"}), name="map.view"),
                path("init/", maps.MapController.as_view({"get": "init_map"}), name="map.init"),
                path("pins/", maps.MapController.as_view({"get": "map_pins_json"}), name="map.pins"),
                path("pins/children/", maps.MapController.as_view({"get": "map_child_pins_json"}), name="map.pins.children"),
                path("pins/meta/", maps.MapController.as_view({"get": "map_pins_meta"}), name="map.pins.meta"),
                path("geolocation/visits/", maps.MapController.as_view({"post": "record_geolocation_visit"}), name="map.geolocation.visits"),
                path("pins/list/", maps.MapController.as_view({"get": "pin_list_panel"}), name="map.pins.list"),
                # Literal "pins/..." routes must be registered before the "pins/<slug:pin_slug>/"
                # catch-all below, which would otherwise match e.g. "pins/bulk-delete/" as a slug.
                path("pins/bulk-delete/", pin_bulk.PinBulkDeleteView.as_view(), name="pin.bulk_delete"),
                path("pins/bulk-undo/", pin_bulk.PinBulkUndoView.as_view(), name="pin.bulk_undo"),
                path("pins/bulk-merge/", pin_bulk.PinBulkMergeView.as_view(), name="pin.bulk_merge"),
                path("pins/bulk-edit/", pin_bulk.PinBulkEditView.as_view(), name="pin.bulk_edit"),
                path(
                    "pins/bulk-edit/badge-options/",
                    pin_bulk.PinBulkEditBadgeOptionsView.as_view(),
                    name="pin.bulk_edit.badge_options",
                ),
                path("pins/<slug:pin_slug>/", maps.MapController.as_view({"get": "map_pin_json"}), name="map.pin.json"),
                path(
                    "boundaries/",
                    boundary.BoundaryController.as_view({"get": "list_boundaries"}),
                    name="boundary.list",
                ),
                path(
                    "add/",
                    maps.MapController.as_view({"post": "post_add_pin"}),
                    name="pin.add",
                ),
                path(
                    "quick-edit/<slug:pin_slug>/",
                    maps.MapController.as_view({"post": "patch_pin"}),
                    name="pin.quick_edit",
                ),
                path(
                    "search/",
                    maps.MapController.as_view({"get": "search_map", "post": "search_map_post"}),
                    name="map.search",
                ),
                path(
                    "search/autocomplete/local/",
                    maps.MapController.as_view({"get": "autocomplete_local"}),
                    name="map.autocomplete.local",
                ),
                path(
                    "search/autocomplete/empty/",
                    maps.MapController.as_view({"get": "autocomplete_empty"}),
                    name="map.autocomplete.empty",
                ),
                path(
                    "search/autocomplete/places/",
                    maps.MapController.as_view({"get": "autocomplete_places"}),
                    name="map.autocomplete.places",
                ),
                path(
                    "search/resolve/",
                    maps.MapController.as_view({"get": "resolve_place"}),
                    name="map.resolve_place",
                ),
                path(
                    "streetview-check/",
                    maps.MapController.as_view({"get": "streetview_check"}),
                    name="map.streetview_check",
                ),
                path(
                    "places/nearby/",
                    maps.MapController.as_view({"get": "nearby_places"}),
                    name="map.places.nearby",
                ),
                path(
                    "media-photo/google-maps/<path:photo_name>/",
                    media_proxy.GoogleMapsPhotoProxyView.as_view(),
                    name="media.google_maps_photo",
                ),
                path(
                    "places/details/",
                    maps.MapController.as_view({"get": "place_details"}),
                    name="map.places.details",
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
                            path("<slug:pin_slug>/share/", pin_sharing.PinShareDialogView.as_view(), name="pin.share.dialog"),
                            path("<slug:pin_slug>/share/send/", pin_sharing.PinShareCreateView.as_view(), name="pin.share.send"),
                            path(
                                "<slug:pin_slug>/boundary/",
                                boundary.BoundaryController.as_view({"get": "get_boundaries", "post": "save_boundary"}),
                                name="boundary.pin",
                            ),
                            path(
                                "<slug:pin_slug>/wiki/create/",
                                wiki_create.PinWikiCreateView.as_view(),
                                name="pin.wiki.create",
                            ),
                            path(
                                "<slug:pin_slug>/media/<str:source>/",
                                pin.PinController.as_view({"get": "media_provider"}),
                                name="pin.media",
                            ),
                            path(
                                "<slug:pin_slug>/media/relevance/",
                                pin.PinController.as_view({"post": "media_relevance"}),
                                name="pin.media.relevance",
                            ),
                            path(
                                "<slug:pin_slug>/media/send-to-wiki/",
                                pin.PinController.as_view({"post": "media_send_to_wiki"}),
                                name="pin.media.send_to_wiki",
                            ),
                            path(
                                "media/sort/",
                                pin.PinController.as_view({"post": "set_media_sort"}),
                                name="pin.media.sort",
                            ),
                            path(
                                "<slug:pin_slug>/cover-photo/",
                                image_gallery.PinCoverPhotoView.as_view(),
                                name="pin.cover_photo",
                            ),
                            path(
                                "<slug:pin_slug>/gallery/bulk/",
                                image_gallery.PinGalleryBulkView.as_view(),
                                name="pin.gallery.bulk",
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
                                "<slug:pin_slug>/search/refresh/",
                                pin.PinController.as_view({"post": "web_search_refresh"}),
                                name="pin.web_search.refresh",
                            ),
                            path(
                                "<slug:pin_slug>/satellite_view/",
                                pin.PinController.as_view({"get": "satellite_view_carousell"}),
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
                                "<slug:pin_slug>/visits/<int:visit_id>/edit/",
                                visits.VisitEditView.as_view(),
                                name="pin.visit.edit",
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
                                "<slug:pin_slug>/detach-parent/",
                                pin_edit.PinDetachChildView.as_view(),
                                name="pin.detach_parent",
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
                                "<slug:pin_slug>/aliases/<int:alias_id>/use/",
                                aliases.PinAliasUseView.as_view(),
                                name="pin.alias.use",
                            ),
                            path(
                                "<slug:pin_slug>/aliases/<int:alias_id>/toggle-nickname/",
                                aliases.PinAliasToggleNicknameView.as_view(),
                                name="pin.alias.toggle_nickname",
                            ),
                            path(
                                "<slug:pin_slug>/custom-fields/",
                                custom_fields.PinCustomFieldsPanelView.as_view(),
                                name="pin.custom_fields",
                            ),
                            path(
                                "<slug:pin_slug>/custom-fields/<int:field_id>/value/",
                                custom_fields.PinCustomFieldValueView.as_view(),
                                name="pin.custom_fields.value",
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
                                "<slug:pin_slug>/yelp/",
                                pin.PinController.as_view({"get": "yelp_info"}),
                                name="pin.yelp",
                            ),
                            path(
                                "<slug:pin_slug>/nominatim/",
                                pin.PinController.as_view({"get": "nominatim_info"}),
                                name="pin.nominatim",
                            ),
                            path(
                                "<slug:pin_slug>/usgs-topo/",
                                pin.PinController.as_view({"get": "usgs_topo_info"}),
                                name="pin.usgs_topo",
                            ),
                            path(
                                "<slug:pin_slug>/debug/clear-cache/",
                                pin.PinController.as_view({"post": "clear_debug_cache"}),
                                name="pin.debug.clear_cache",
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
                                "<slug:pin_slug>/immich/search/",
                                immich.PinImmichSearchView.as_view(),
                                name="pin.immich.search",
                            ),
                            path(
                                "<slug:pin_slug>/immich/thumbnail/<str:asset_id>/",
                                immich.PinImmichThumbnailView.as_view(),
                                name="pin.immich.thumbnail",
                            ),
                            path(
                                "<slug:pin_slug>/immich/import/",
                                immich.PinImmichImportView.as_view(),
                                name="pin.immich.import",
                            ),
                            path(
                                "<slug:pin_slug>/immich/import/<str:task_id>/progress/",
                                immich.PinImmichImportProgressView.as_view(),
                                name="pin.immich.import.progress",
                            ),
                            path(
                                "<slug:pin_slug>/flickr/search/",
                                flickr.PinFlickrSearchView.as_view(),
                                name="pin.flickr.search",
                            ),
                            path(
                                "<slug:pin_slug>/flickr/import/",
                                flickr.PinFlickrImportView.as_view(),
                                name="pin.flickr.import",
                            ),
                            path(
                                "<slug:pin_slug>/flickr/import/<str:task_id>/progress/",
                                flickr.PinFlickrImportProgressView.as_view(),
                                name="pin.flickr.import.progress",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/",
                                google_photos.PinGooglePhotosStartView.as_view(),
                                name="pin.google_photos.start",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/session/",
                                google_photos.PinGooglePhotosSessionCreateView.as_view(),
                                name="pin.google_photos.session.create",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/session/<str:session_id>/status/",
                                google_photos.PinGooglePhotosSessionStatusView.as_view(),
                                name="pin.google_photos.session.status",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/thumbnail/<str:session_id>/<str:item_id>/",
                                google_photos.PinGooglePhotosThumbnailView.as_view(),
                                name="pin.google_photos.thumbnail",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/import/",
                                google_photos.PinGooglePhotosImportView.as_view(),
                                name="pin.google_photos.import",
                            ),
                            path(
                                "<slug:pin_slug>/google-photos/import/<str:task_id>/progress/",
                                google_photos.PinGooglePhotosImportProgressView.as_view(),
                                name="pin.google_photos.import.progress",
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
        "saved-filters/",
        include(
            [
                path("create/", saved_filters.SavedFilterCreateView.as_view(), name="saved_filters.create"),
                path("<uuid:filter_uuid>/delete/", saved_filters.SavedFilterDeleteView.as_view(), name="saved_filters.delete"),
            ],
        ),
    ),
    path(
        "lists/",
        include(
            [
                path("", pin_lists.PinListsIndexView.as_view(), name="lists.list"),
                path("create/", pin_lists.PinListCreateView.as_view(), name="lists.create"),
                path("<uuid:list_uuid>/", pin_lists.PinListDetailView.as_view(), name="lists.detail"),
                path("<uuid:list_uuid>/edit/", pin_lists.PinListEditView.as_view(), name="lists.edit"),
                path("<uuid:list_uuid>/delete/", pin_lists.PinListDeleteView.as_view(), name="lists.delete"),
                path("<uuid:list_uuid>/items/", pin_lists.PinListItemsView.as_view(), name="lists.items"),
                path("<uuid:list_uuid>/items/add/", pin_lists.PinListAddPinsView.as_view(), name="lists.items.add"),
                path("<uuid:list_uuid>/items/<int:item_id>/remove/", pin_lists.PinListRemoveItemView.as_view(), name="lists.items.remove"),
                path("<uuid:list_uuid>/items/reorder/", pin_lists.PinListReorderView.as_view(), name="lists.items.reorder"),
                path("<uuid:list_uuid>/create-trip/", pin_lists.PinListCreateTripView.as_view(), name="lists.create_trip"),
                path("<uuid:list_uuid>/add-to-trip/", pin_lists.PinListAddToTripView.as_view(), name="lists.add_to_trip"),
                path("<uuid:list_uuid>/markup-map/", pin_lists.PinListMarkupMapView.as_view(), name="lists.markup_map"),
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
                path(
                    "edit/emails/verify/<uuid:token>/",
                    userprofile.ProfileEmailVerifyView.as_view(),
                    name="profile.email.verify",
                ),
                path("preview/exit/", userprofile.ProfilePreviewStopView.as_view(), name="profile.preview.exit"),
                path("preview/<slug:mode>/", userprofile.ProfilePreviewStartView.as_view(), name="profile.preview"),
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
                path(
                    "<slug:profile_slug>/custom-field/<int:field_id>/",
                    custom_fields.ProfileCustomFieldValueView.as_view(),
                    name="profile.custom_field_value",
                ),
            ],
        ),
    ),
    path("settings/", settings.SettingsView.as_view(), name="settings.view"),
    path("settings/custom-fields/", custom_fields.CustomFieldSettingsPanelView.as_view(), name="custom_fields.settings"),
    path("settings/custom-fields/<int:field_id>/", custom_fields.CustomFieldUpdateView.as_view(), name="custom_fields.update"),
    path("settings/custom-fields/<int:field_id>/delete/", custom_fields.CustomFieldDeleteView.as_view(), name="custom_fields.delete"),
    path("custom-fields/photo/<int:image_id>/", custom_fields.PhotoCustomFieldsView.as_view(), name="custom_fields.photo"),
    path("custom-fields/markup-map/<uuid:map_uuid>/", custom_fields.MarkupMapCustomFieldsView.as_view(), name="custom_fields.markup_map"),
    path("settings/geocode/", settings.geocode_address, name="settings.geocode"),
    path("settings/map-position/", settings.SaveMapPositionView.as_view(), name="settings.save_map_position"),
    path("settings/map-dark-mode/", settings.SaveMapDarkModeView.as_view(), name="settings.save_map_dark_mode"),
    path("settings/delete-account/", account_deletion.RequestAccountDeletionView.as_view(), name="account.delete.request"),
    path("settings/delete-account/cancel/", account_deletion.CancelAccountDeletionView.as_view(), name="account.delete.cancel"),
    path("settings/undo-history/", undo.UndoHistoryView.as_view(), name="undo.history"),
    path("settings/undo-history/clear/", undo.UndoClearView.as_view(), name="undo.clear"),
    path("settings/immich/", immich.ImmichSettingsView.as_view(), name="settings.immich"),
    path("settings/immich/disconnect/", immich.ImmichDisconnectView.as_view(), name="settings.immich.disconnect"),
    path("settings/flickr/", flickr.FlickrSettingsView.as_view(), name="settings.flickr"),
    path("settings/flickr/connect/", flickr.FlickrConnectView.as_view(), name="settings.flickr.connect"),
    path("settings/flickr/callback/", flickr.FlickrCallbackView.as_view(), name="settings.flickr.callback"),
    path("settings/flickr/disconnect/", flickr.FlickrDisconnectView.as_view(), name="settings.flickr.disconnect"),
    path("settings/google-photos/", google_photos.GooglePhotosSettingsView.as_view(), name="settings.google_photos"),
    path("settings/google-photos/connect/", google_photos.GooglePhotosConnectView.as_view(), name="settings.google_photos.connect"),
    path("settings/google-photos/callback/", google_photos.GooglePhotosCallbackView.as_view(), name="settings.google_photos.callback"),
    path("settings/google-photos/disconnect/", google_photos.GooglePhotosDisconnectView.as_view(), name="settings.google_photos.disconnect"),
    path("undo/<uuid:undo_id>/restore/", undo.UndoRestoreView.as_view(), name="undo.restore"),
    re_path(
        r"^(?P<badge_kind>tags?|categor(y|ies)|status(es)?|people)/",
        include(
            [
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
            ]
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
                    "<slug:location_slug>/wiki/delete/",
                    location_wiki.LocationWikiDeleteView.as_view(),
                    name="location.wiki.delete",
                ),
                path(
                    "<slug:location_slug>/wiki/edit/",
                    location_wiki.LocationWikiEditView.as_view(),
                    name="location.wiki.edit",
                ),
                path(
                    "<slug:location_slug>/wiki/boundary/",
                    boundary.WikiBoundaryView.as_view(),
                    name="location.wiki.boundary",
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
                    "<slug:location_slug>/wiki/history/<int:edit_id>/delete/",
                    location_wiki.LocationWikiEditDeleteView.as_view(),
                    name="location.wiki.history.delete",
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
                    "<slug:location_slug>/wiki/detail-pins/<uuid:detail_pin_uuid>/",
                    detail_pins.LocationWikiDetailPinEditView.as_view(),
                    name="location.wiki.detail_pin.edit",
                ),
                path(
                    "<slug:location_slug>/wiki/markup/json/",
                    markup.MarkupJsonView.as_view(),
                    name="location.wiki.markup.json",
                ),
                path(
                    "<slug:location_slug>/wiki/markup/",
                    markup.MarkupView.as_view(),
                    name="location.wiki.markup",
                ),
                path(
                    "<slug:location_slug>/wiki/markup/<uuid:markup_uuid>/",
                    markup.MarkupEditView.as_view(),
                    name="location.wiki.markup.edit",
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
                    "<slug:location_slug>/wiki/aliases/<int:alias_id>/use/",
                    aliases.LocationAliasUseView.as_view(),
                    name="location.wiki.alias.use",
                ),
                path(
                    "<slug:location_slug>/wiki/aliases/<int:alias_id>/toggle-nickname/",
                    aliases.LocationAliasToggleNicknameView.as_view(),
                    name="location.wiki.alias.toggle_nickname",
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
                path(
                    "<slug:location_slug>/wiki/cover-photo/",
                    image_gallery.WikiCoverPhotoView.as_view(),
                    name="location.wiki.cover_photo",
                ),
                path(
                    "<slug:location_slug>/wiki/stat/<str:field>/vote/",
                    location_wiki.WikiStatVoteView.as_view(),
                    name="location.wiki.stat_vote",
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
                path("calendar/connect/", calendar_sync.GoogleCalendarConnectView.as_view(), name="trips.calendar.connect"),
                path("calendar/callback/", calendar_sync.GoogleCalendarCallbackView.as_view(), name="trips.calendar.callback"),
                path("calendar/disconnect/", calendar_sync.GoogleCalendarDisconnectView.as_view(), name="trips.calendar.disconnect"),
                path("calendar/import/", calendar_sync.CalendarImportView.as_view(), name="trips.calendar.import"),
                path("calendar/import/preview/", calendar_sync.CalendarImportPreviewView.as_view(), name="trips.calendar.import.preview"),
                path("<uuid:trip_uuid>/calendar/export/", calendar_sync.TripCalendarExportView.as_view(), name="trips.calendar.export"),
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
                path("<uuid:trip_uuid>/join/", trip.TripMembershipJoinView.as_view(), name="trips.join"),
                path("<uuid:trip_uuid>/join", trip.TripMembershipJoinView.as_view()),
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
        "safety/",
        include(
            [
                path("", safety.SafetyHomeView.as_view(), name="safety.home"),
                path("new/", safety.SafetyCheckinCreateView.as_view(), name="safety.checkin.create"),
                path("nav-banner/", safety.SafetyActiveCheckinBannerView.as_view(), name="safety.active_banner"),
                path("wiki-option/", safety.SafetyCheckinWikiOptionView.as_view(), name="safety.checkin.wiki_option"),
                path("contact/<uuid:token>/", safety.SafetyContactPortalView.as_view(), name="safety.contact.portal"),
                path("contact/<uuid:token>/mark-safe/", safety.SafetyContactMarkSafeView.as_view(), name="safety.contact.mark_safe"),
                path("contact/<uuid:token>/opt-out/<str:scope>/", safety.SafetyContactOptOutView.as_view(), name="safety.contact.optout"),
                path("contact/<uuid:token>/messages/", safety.SafetyCheckinMessageView.as_view(), name="safety.contact.messages"),
                path("contact/<uuid:token>/markup/json/", markup.SafetyContactMarkupJsonView.as_view(), name="safety.contact.markup.json"),
                path("<slug:checkin_slug>/", safety.SafetyCheckinDetailView.as_view(), name="safety.checkin.detail"),
                path("<uuid:checkin_uuid>/cancel/", safety.SafetyCheckinCancelView.as_view(), name="safety.checkin.cancel"),
                path("<slug:checkin_slug>/checkin/", safety.SafetyCheckinCheckInView.as_view(), name="safety.checkin.checkin"),
                path("<uuid:checkin_uuid>/messages/", safety.SafetyCheckinMessageView.as_view(), name="safety.checkin.messages"),
                path("<slug:checkin_slug>/gallery/", safety.SafetyGalleryView.as_view(), name="safety.checkin.gallery"),
                path("<slug:checkin_slug>/gallery/<int:image_id>/", safety.SafetyImageView.as_view(), name="safety.checkin.gallery.image"),
                # No-trailing-slash variant so DELETE/POST fetch calls work even
                # when APPEND_SLASH would otherwise downgrade the method to GET.
                path("<slug:checkin_slug>/gallery/<int:image_id>", safety.SafetyImageView.as_view()),
                path("<slug:checkin_slug>/delete/", safety.SafetyCheckinDeleteView.as_view(), name="safety.checkin.delete"),
                path("<slug:checkin_slug>/maps/picker/", safety.SafetyCheckinMapPickerView.as_view(), name="safety.checkin.maps.picker"),
                path("<slug:checkin_slug>/maps/attach/", safety.SafetyCheckinMapAttachView.as_view(), name="safety.checkin.maps.attach"),
                path("<slug:checkin_slug>/maps/<uuid:map_uuid>/detach/", safety.SafetyCheckinMapDetachView.as_view(), name="safety.checkin.maps.detach"),
            ],
        ),
    ),
    path(
        "markup-maps/",
        include(
            [
                path("new/", markup.MarkupMapCreateView.as_view(), name="markup_map.create"),
                path("<uuid:map_uuid>/json/", markup.MarkupJsonView.as_view(), name="markup_map.json"),
                path("<uuid:map_uuid>/snapshot/", markup.MarkupMapSnapshotView.as_view(), name="markup_map.snapshot"),
                path("<uuid:map_uuid>/view/", markup.MarkupMapViewStateView.as_view(), name="markup_map.view_state"),
                path("<uuid:map_uuid>/delete/", markup.MarkupMapDeleteView.as_view(), name="markup_map.delete"),
                path("<uuid:map_uuid>/clone/", markup.MarkupMapCloneView.as_view(), name="markup_map.clone"),
                path("<uuid:map_uuid>/share/", map_sharing.MarkupMapShareDialogView.as_view(), name="markup_map.share.dialog"),
                path("<uuid:map_uuid>/share/send/", map_sharing.MarkupMapShareCreateView.as_view(), name="markup_map.share.send"),
                path("<uuid:map_uuid>/markup/", markup.MarkupView.as_view(), name="markup_map.markup"),
                path("<uuid:map_uuid>/markup/<uuid:markup_uuid>/", markup.MarkupEditView.as_view(), name="markup_map.markup.edit"),
            ],
        ),
    ),
    path(
        "organize/",
        include(
            [
                path("", organize.OrganizeIndexView.as_view(), name="organize.index"),
                path("priority/save/", organize.OrganizePrioritySaveView.as_view(), name="organize.priority.save"),
                path("priority/list/", organize.OrganizePriorityListView.as_view(), name="organize.priority.list"),
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
    path("pin-shares/<int:share_id>/", pin_sharing.PinShareDetailView.as_view(), name="pin.share.detail"),
    path("pin-shares/<int:share_id>/respond/", pin_sharing.PinShareRespondView.as_view(), name="pin.share.respond"),
    path("map-shares/<int:share_id>/", map_sharing.MarkupMapShareDetailView.as_view(), name="markup_map.share.detail"),
    path("visit-suggestions/<int:suggestion_id>/respond/", visit_suggestions.VisitSuggestionRespondView.as_view(), name="visit_suggestion.respond"),
    path(
        "messages/",
        include(
            [
                path("", direct_messages.MessagesPageView.as_view(), name="messages.view"),
                path("dropdown/", direct_messages.MessagesDropdownView.as_view(), name="messages.dropdown"),
                path("unread-count/", direct_messages.MessagesUnreadCountView.as_view(), name="messages.unread_count"),
                path("recipients/", direct_messages.RecipientSearchView.as_view(), name="messages.recipients"),
                path("upload-image/", direct_messages.DirectMessageImageUploadView.as_view(), name="messages.upload_image"),
                path("attach-map/picker/", direct_messages.DirectMessageMapPickerView.as_view(), name="messages.attach_map.picker"),
                path("list/", direct_messages.ConversationListView.as_view(), name="messages.list"),
                path("<slug:profile_slug>/", direct_messages.ConversationView.as_view(), name="messages.conversation"),
                path("<slug:profile_slug>/send/", direct_messages.ConversationSendView.as_view(), name="messages.send"),
                path("<slug:profile_slug>/read/", direct_messages.ConversationReadView.as_view(), name="messages.read"),
                path("<slug:profile_slug>/react/<int:message_id>/", direct_messages.MessageReactionToggleView.as_view(), name="messages.react"),
                path("<slug:profile_slug>/delete/<int:message_id>/", direct_messages.MessageDeleteView.as_view(), name="messages.delete"),
                path("<slug:profile_slug>/image-permission/", direct_messages.MessageImagePermissionView.as_view(), name="messages.image_permission"),
                path("<slug:profile_slug>/share/pin/", direct_message_shares.MessageSharePinView.as_view(), name="messages.share.pin"),
                path("<slug:profile_slug>/share/trip/", direct_message_shares.MessageShareTripView.as_view(), name="messages.share.trip"),
                path("<slug:profile_slug>/share/friend/", direct_message_shares.MessageShareFriendView.as_view(), name="messages.share.friend"),
            ],
        ),
    ),
    path(
        "e2ee/",
        include(
            [
                path("login-params/", e2ee.E2EELoginParamsView.as_view(), name="e2ee.login_params"),
                path("enroll/", e2ee.E2EEEnrollView.as_view(), name="e2ee.enroll"),
                path("keys/", e2ee.E2EEOwnKeysView.as_view(), name="e2ee.keys"),
                path("keys/<slug:profile_slug>/", e2ee.E2EEPartnerKeyView.as_view(), name="e2ee.partner_key"),
                path("conversation-key/<slug:profile_slug>/", e2ee.E2EEConversationKeyView.as_view(), name="e2ee.conversation_key"),
                path("rewrap/", e2ee.E2EERewrapView.as_view(), name="e2ee.rewrap"),
                path("reset/", e2ee.E2EEResetView.as_view(), name="e2ee.reset"),
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
                path("admin/", tools.AdminToolsView.as_view(), name="tools.admin.index"),
                path("export/start/", tools.ExportStartView.as_view(), name="tools.export.start"),
                path("export/status/<str:job_id>/", tools.ExportStatusView.as_view(), name="tools.export.status"),
                path("export/download/<str:job_id>/", tools.ExportDownloadView.as_view(), name="tools.export.download"),
                path("import/start/", tools.ImportStartView.as_view(), name="tools.import.start"),
                path("import/status/<str:job_id>/", tools.ImportStatusView.as_view(), name="tools.import.status"),
                path("backup/start/", tools.BackupStartView.as_view(), name="tools.backup.start"),
            ],
        ),
    ),
    path(
        "memories/",
        include(
            [
                path("", memories.MemoriesView.as_view(), name="memories.view"),
                path("data/", memories.MemoriesFeedDataView.as_view(), name="memories.data"),
                path("on-this-day/", memories.MemoriesOnThisDayView.as_view(), name="memories.on_this_day"),
                path("hero-stats/", memories.MemoriesHeroStatsView.as_view(), name="memories.hero_stats"),
                path("visit/<slug:pin_slug>/", memories.MemoriesVisitView.as_view(), name="memories.visit"),
                path("visit/<slug:pin_slug>/<int:visit_id>/", memories.MemoriesVisitView.as_view(), name="memories.visit.edit"),
                path("visits/", memories.MemoriesVisitsView.as_view(), name="memories.visits"),
                path("maps/", memories.MemoriesMapsView.as_view(), name="memories.maps"),
                path("sharing/", memories.MemoriesSharingView.as_view(), name="memories.sharing"),
                path("unlogged/<slug:pin_slug>/<str:action>/", memories.MemoriesUnloggedActionView.as_view(), name="memories.unlogged.action"),
                path("photos/", photos.MemoriesPhotosView.as_view(), name="memories.photos"),
                path("photos/queue/", photos.PhotoQueueView.as_view(), name="memories.photos.queue"),
                path("photos/page/", photos.PhotoGridPageView.as_view(), name="memories.photos.page"),
                path("photos/upload/", photos.PhotoUploadView.as_view(), name="memories.photos.upload"),
                path("photos/pin-search/", photos.PhotoPinSearchView.as_view(), name="memories.photos.pin_search"),
                path("photos/<int:image_id>/confirm-pin/", photos.PhotoPinConfirmView.as_view(), name="memories.photos.pin_confirm"),
                path("photos/<int:image_id>/<str:action>/", photos.PhotoActionView.as_view(), name="memories.photos.action"),
            ],
        ),
    ),
    path("setup/", setup.SetupWizardView.as_view(), name="setup"),
    path("welcome/", onboarding.WelcomeOnboardingView.as_view(), name="onboarding.welcome"),
    path("tasks/<str:task_id>/status/", site_admin.CeleryTaskStatusView.as_view(), name="celery_task_status"),
    path("site-admin/", site_admin.SiteAdminHomeView.as_view(), name="site_admin_home"),
    path("site-admin/users/", site_admin.SiteAdminUsersView.as_view(), name="site_admin_users"),
    path("site-admin/settings/", site_admin.SiteAdminView.as_view(), name="site_admin"),
    path("site-admin/stats/", site_admin.SiteAdminStatsView.as_view(), name="site_admin_stats"),
    path("site-admin/stats/kpi/", site_admin.SiteAdminStatsKpiPartialView.as_view(), name="site_admin_stats_kpi"),
    path("site-admin/stats/system/", site_admin.SiteAdminStatsSystemPartialView.as_view(), name="site_admin_stats_system"),
    path("site-admin/stats/api/", site_admin.SiteAdminStatsApiUsagePartialView.as_view(), name="site_admin_stats_api"),
    path("site-admin/stats/pull-latest-code/", site_admin.SiteAdminPullLatestCodeView.as_view(), name="site_admin_pull_latest_code"),
    path("site-admin/subscriptions/", site_admin.SiteAdminSubscriptionsView.as_view(), name="site_admin_subscriptions"),
    path("site-admin/api-limits/", site_admin.SiteAdminApiLimitsView.as_view(), name="site_admin_api_limits"),
    path("site-admin/plugins/", site_admin.SiteAdminPluginsView.as_view(), name="site_admin_plugins"),
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
