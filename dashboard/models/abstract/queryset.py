"""

	Metadata:

		File: queryset.py
		Project: UrbanLens
		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
from typing import TYPE_CHECKING
import logging
# Django Imports
from djangofoundry import models
# Lib Imports
# App Imports

# Typechecking imports
if TYPE_CHECKING:
	pass

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class QuerySet(models.QuerySet):
	'''
	A custom queryset. All models below will use this for interacting with results from the db.
	'''

class Manager(models.PostgresManager.from_queryset(QuerySet)):
	'''
	A custom query manager. This creates QuerySets and is used in all models interacting with the app db.
	'''
