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
*        Path:    /urls.py                                                                                             *
*        Project: dashboard                                                                                            *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
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
from django.shortcuts import redirect
# Django imports
from django.urls import path, include, re_path
# 3rd Party imports
from rest_framework import routers
from dashboard.models.categories import CategoryViewSet
from dashboard.models.locations import LocationViewSet
from dashboard.models.comments import CommentViewSet
from dashboard.models.images import ImageViewSet
from dashboard.models.profile import ProfileViewSet
from dashboard.controllers import MapController

logger = logging.getLogger(__name__)

app_name = 'dashboard'

# Define all our REST API routes
routes = {
	'categories': CategoryViewSet,
	'locations': LocationViewSet,
	'profiles': ProfileViewSet,
	'comments': CommentViewSet,
	'images': ImageViewSet
}
# Use the default router to define endpoints

router = routers.DefaultRouter()

# Register each viewset with the router
for route, viewset in routes.items():
	if hasattr(viewset, 'basename'):
		router.register(route, viewset, basename = getattr(viewset, 'basename'))
	else:
		router.register(route, viewset)

urlpatterns = [
	path('rest/', include(router.urls)),
	path('map/', MapController.view_map, name='view_map'),
	path('map/edit/<int:pin_id>/', MapController.edit_pin, name='edit_pin'),
	path('map/add/', MapController.add_pin, name='add_pin'),
	path('map/search/', MapController.search_pins, name='search_pins'),
	path('map/upload_image/<int:location_id>/', MapController.upload_image, name='upload_image'),
	path('map/change_category/<int:location_id>/', MapController.change_category, name='change_category'),
	from dashboard.controllers import MapController, ProfileController

	re_path(r'^.*$', lambda request, exception: redirect('/'), name='404'),
	path('profile/', ProfileController.view_profile, name='view_profile'),
	path('profile/edit/', ProfileController.edit_profile, name='edit_profile'),
	path('map/advanced_search/', MapController.advanced_search, name='advanced_search'),
	path('map/add_review/<int:location_id>/', MapController.add_review, name='add_review')
]
from django.urls import path
from . import views

urlpatterns = [
    path('friendship/request', views.request_friend, name='request_friend'),
    path('friendship/list', views.list_friends, name='list_friends'),
    # other paths...
]
