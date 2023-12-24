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

logger = logging.getLogger(__name__)

app_name = 'dashboard'

# Define all our REST API routes
routes = {
	'categories': CategoryViewSet,
	'locations': LocationViewSet,
	'profiles': ProfileViewSet,
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
	#path('api/locations', locations.LocationViewSet.as_view({'get': 'list'}), name='locations'),
	#path('api/login', login, name='login'),
	#path('api/logout', logout, name='logout'),

	# Otherwise, return 404
	re_path(r'^.*$', lambda request, exception: redirect('/'), name='404')
]
