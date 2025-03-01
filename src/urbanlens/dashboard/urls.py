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
from django.urls import path, include, re_path
from django.views.generic import TemplateView
# 3rd Party imports
from rest_framework import routers
#from urbanlens.dashboard.models.categories import CategoryViewSet
from urbanlens.dashboard.models.pin import PinViewSet
#from urbanlens.dashboard.models.comments import CommentViewSet
#from urbanlens.dashboard.models.images import ImageViewSet
from urbanlens.dashboard.models.reviews import ReviewViewSet
from urbanlens.dashboard.models.profile import ProfileViewSet
from urbanlens.dashboard.controllers import friendship, map, pin, profile
from urbanlens.dashboard.controllers.index import IndexController

logger = logging.getLogger(__name__)

app_name = 'dashboard'

# Define all our REST API routes
routes = {
	#'categories': CategoryViewSet,
	'pins': PinViewSet,
	'profiles': ProfileViewSet,
	'reviews': ReviewViewSet,
	#'comments': CommentViewSet,
	#'images': ImageViewSet
}
router = routers.DefaultRouter()

# Register each viewset with the router
for route, viewset in routes.items():
	if hasattr(viewset, 'basename'):
		router.register(route, viewset, basename = getattr(viewset, 'basename'))
	else:
		router.register(route, viewset)

urlpatterns = [
    path('rest/reviews/create_or_update/<int:pk>/', ReviewViewSet.as_view({'patch': 'create_or_update'}), name='review-create-or-update'),
	path('rest/', include(router.urls)),
	re_path('^$', IndexController.as_view(), name='home'),
	path('map/', include([
		path('', map.MapController.as_view({'get': 'view_map'}), name='view_map'),
		path('init/', map.MapController.as_view({'get': 'init_map'}), name='init_map'),
		path('add/', map.MapController.as_view({'get': 'add_pin', 'post': 'post_add_pin'}), name='add_pin'),
		path('edit/<int:pin_id>/', map.MapController.as_view({'get': 'get_edit_pin', 'post': 'edit_pin'}), name='edit_pin'),
		path('search/', map.MapController.as_view({'get': 'search_map', 'post': 'search_map_post'}), name='search_map'),
		path('upload_image/<int:pin_id>/', map.MapController.as_view({'post': 'upload_image'}), name='upload_image'),
		path('change_category/<int:pin_id>/', map.MapController.as_view({'post': 'change_category'}), name='change_category'),
		#path('delete/<int:pin_id>/', MapController.delete_pin, name='delete_pin'),
		#path('add_review/<int:pin_id>/', map.MapController.as_view(), name='add_review'),
		path('pin/', include([
			path('<int:pin_id>/', pin.PinController.as_view({'get': 'view'}), name='pin.details'),
			path('<int:pin_id>/smithsonian/', pin.PinController.as_view({'get': 'get_smithsonian_images'}), name='smithsonian_images'),
			path('<int:pin_id>/google/', pin.PinController.as_view({'get': 'get_google_images'}), name='google_images'),
			path('<int:pin_id>/search/', pin.PinController.as_view({'get': 'web_search'}), name='pin.web_search'),
    		path('<int:pin_id>/satellite_view/', pin.PinController.as_view({'get': 'satellite_view_google_image'}), name='pin.satellite_view'),
    		path('<int:pin_id>/street_view/', pin.PinController.as_view({'get': 'street_view'}), name='pin.street_view'),
			path('<int:pin_id>/weather/', pin.PinController.as_view({'get': 'weather_forecast'}), name='pin.weather_forecast'),
			path('import/', include([
				path('form/', pin.PinController.as_view({'get': 'import_form'}), name='pin.import.form'),
				path('upload/', pin.PinController.as_view({'post': 'upload_takeout'}), name='pin.upload.takeout'),
			])),
		])),
	])),
	path('profile/', include([
		path('', profile.ViewProfileView.as_view(), name='view_profile'),
		path('edit/', profile.EditProfileView.as_view(), name='profile.edit'),
	])),
	path('friendship/', include([
		path('list/<int:profile_id>', friendship.FriendController.as_view({'get':'friend_list'}), name='friend.list'),
		path('request/<int:profile_id>', friendship.FriendController.as_view({'post': 'request_friend'}), name='friend.request'),
		path('accept/<int:profile_id>', friendship.FriendController.as_view({'post': 'accept_friend'}), name='friend.accept'),
		path('reject/<int:profile_id>', friendship.FriendController.as_view({'post': 'reject_friend'}), name='friend.reject'),
		path('remove/<int:profile_id>', friendship.FriendController.as_view({'post': 'remove_friend'}), name='friend.remove'),
		path('block/<int:profile_id>', friendship.FriendController.as_view({'post': 'block_friend'}), name='friend.block'),
		path('mute/<int:profile_id>', friendship.FriendController.as_view({'post': 'mute_friend'}), name='friend.mute'),
	])),

	path('test_ai/', pin.PinController.as_view({'get': 'test_ai'}), name='test_ai'),

	path('', include('social_django.urls', namespace='social')),
	re_path('.*', TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name='404')
]
