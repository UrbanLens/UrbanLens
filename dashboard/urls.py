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
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
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
#from dashboard.models.categories import CategoryViewSet
from dashboard.models.locations import LocationViewSet
#from dashboard.models.comments import CommentViewSet
#from dashboard.models.images import ImageViewSet
from dashboard.models.profile import ProfileViewSet
from dashboard.controllers import friendship, map, location, profile
from dashboard.controllers.index import IndexController

logger = logging.getLogger(__name__)

app_name = 'dashboard'

# Define all our REST API routes
routes = {
	#'categories': CategoryViewSet,
	'locations': LocationViewSet,
	'profiles': ProfileViewSet,
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
	path('rest/', include(router.urls)),
	re_path('^$', IndexController.as_view(), name='home'),
	path('map/', include([
		path('', map.MapController.as_view({'get': 'view_map'}), name='view_map'),
		path('init/', map.MapController.as_view({'get': 'init_map'}), name='init_map'),
		path('add/', map.MapController.as_view({'get': 'add_pin', 'post': 'post_add_pin'}), name='add_pin'),
		path('edit/<int:pin_id>/', map.MapController.as_view({'get': 'get_edit_pin', 'post': 'edit_pin'}), name='edit_pin'),
		path('search/', map.MapController.as_view({'get': 'search_pins'}), name='search_pins'),
		path('upload_image/<int:location_id>/', map.MapController.as_view({'post': 'upload_image'}), name='upload_image'),
		path('change_category/<int:location_id>/', map.MapController.as_view({'post': 'change_category'}), name='change_category'),
		#path('delete/<int:location_id>/', MapController.delete_pin, name='delete_pin'),
		#path('add_review/<int:location_id>/', map.MapController.as_view(), name='add_review'),
		path('location/<int:location_id>/', location.LocationController.as_view({'get': 'view'}), name='view_location'),
	])),
	path('profile/', include([
		path('', profile.ViewProfileView.as_view(), name='view_profile'),
		path('edit/', profile.EditProfileView.as_view(), name='edit_profile'),
	])),
	path('friendship/', include([
		path('list/', friendship.ListFriendsView.as_view(), name='list_friends'),
		#path('accept/<int:profile_id>', FriendshipController.accept_friend, name='accept_friend'),
		#path('reject/<int:profile_id>', FriendshipController.reject_friend, name='reject_friend'),
		path('request/<int:profile_id>', friendship.RequestFriendView.as_view(), name='request_friend'),
	])),
	path('', include('social_django.urls', namespace='social')),
	re_path('.*', TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name='404')
]
