"""

	Metadata:
		File: abstract.py
		Project: UrbanLens
		Author: Jess Mann
		Contact: jess@manlyphotos.com
		-----
		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
import logging
# Django Imports
# Lib Imports
from djangofoundry.models import Model as FoundryModel
from djangofoundry.models.fields import InsertedNowField, UpdatedNowField
# App Imports
from dashboard.models.abstract.queue import Queue
from dashboard.models.abstract.queryset import Manager


#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class Model(FoundryModel):
	'''
	A base model that all other models in this app inherit from.
	'''
	created = InsertedNowField()
	updated = UpdatedNowField()
	queue: Queue = Queue()
	objects: Manager = Manager()

	class Meta(FoundryModel.Meta):
		"""
		Metadata about this model (such as the table name)

		Attributes:
			db_table (str):
				The name of the table in the DB
			unique_together (list of str):
				A list of attributes which form unique keys
			indexes (list of Index):
				A list of indexes to create on the table
		"""
		abstract = True
		app_label = 'dashboard'