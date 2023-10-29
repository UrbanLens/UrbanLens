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
# Django imports
from django.urls import path, include, re_path
# 3rd Party imports
from rest_framework import routers
from dashboard.controllers.svelte import SvelteController
from dashboard.models import categories, comments, images, locations, profile

app_name = 'dashboard'

# Define all our REST API routes
routes = {
	'categories': categories.CategoryViewSet,
	'comments': comments.CommentViewSet,
	'images': images.ImageViewSet,
	'locations': locations.LocationViewSet,
	'profile': profile.ProfileViewSet,
}
# Use the default router to define endpoints
router = routers.DefaultRouter()

# Register each viewset with the router
for route, viewset in routes.items():
	if hasattr(viewset, 'basename'):
		router.register(route, viewset, basename = getattr(viewset, 'basename'))
	else:
		router.register(route, viewset)

from dashboard.views import login, logout

urlpatterns = [
	path('rest/', include(router.urls)),
	path('api/login', login, name='login'),
	path('api/logout', logout, name='logout'),
	
	# Send everything else to svelte
	re_path(r'^.*$', SvelteController.as_view(), name="svelte"),
]
