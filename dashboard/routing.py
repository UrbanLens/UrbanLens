"""

	Metadata:

		File: routers.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 Urban Lens
"""
from django.urls import path
from .consumers import RequestStatusConsumer

websocket_urlpatterns = [
	path('ws/request_status/', RequestStatusConsumer.as_asgi()),
]
