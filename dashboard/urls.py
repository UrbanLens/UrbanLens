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
# App Imports
from dashboard.controllers.home import IndexController

app_name = 'dashboard'

# Define all our REST API routes
routes = {
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
	# ex: /api/
	path('rest/', include(router.urls)),

	# Send everything else to default
	re_path(r'^.*$', IndexController.as_view(), name="home"),
]
