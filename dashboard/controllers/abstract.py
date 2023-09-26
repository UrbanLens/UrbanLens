"""

	Metadata:

		File: abstract.py
		Project: UrbanLens
		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
import os
import logging
import random
import time
# Django imports
from django.http import HttpResponse
from rest_framework import viewsets
from djangofoundry.controllers import DetailController as LibDetailController
from djangofoundry.controllers import ListController as LibListController
from djangofoundry.mixins import HasParams
from djangofoundry import models

# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)
