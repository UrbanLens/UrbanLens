"""

	Metadata:

		File: apps.py
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
from django.apps import AppConfig

class DashboardConfig(AppConfig):
	default_auto_field = 'django.db.models.BigAutoField'
	name = 'dashboard'