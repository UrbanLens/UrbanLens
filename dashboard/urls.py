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

app_name = 'dashboard'