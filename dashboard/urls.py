"""


	Metadata:

		File: urls.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 UrbanLens

"""
# Generic imports
from __future__ import annotations
import logging
# Django imports
from django.urls import path, include, re_path
# 3rd Party imports
from rest_framework import routers
from dashboard.controllers.svelte import SvelteController
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
	'comments': CommentViewSet,
	'images': ImageViewSet,
	'locations': LocationViewSet,
	'profiles': ProfileViewSet,
}
# Use the default router to define endpoints

router = routers.DefaultRouter()

# Register each viewset with the router
for route, viewset in routes.items():
	if hasattr(viewset, 'basename'):
		logger.critical('Adding route %s with basename %s', route, getattr(viewset, 'basename'))
		router.register(route, viewset, basename = getattr(viewset, 'basename'))
	else:
		logger.critical('Adding route %s', route)
		router.register(route, viewset)

urlpatterns = [
	path('rest/', include(router.urls)),
	path('api/locations', locations.LocationViewSet.as_view({'get': 'list'}), name='locations'),
	#path('api/login', login, name='login'),
	#path('api/logout', logout, name='logout'),

	# Send everything else to svelte
	re_path(r'^.*$', SvelteController.as_view(), name="svelte"),
]
