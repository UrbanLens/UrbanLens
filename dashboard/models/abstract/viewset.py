"""

	Metadata:

		File: viewset.py
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
from typing import TYPE_CHECKING
# Django Imports
# Third party imports
# Lib imports
from djangofoundry.models import viewset
# App imports
from dashboard.models.abstract.serializer import Serializer

if TYPE_CHECKING:
	pass

class ViewSet(viewset.ViewSet):
	serializer_class = Serializer