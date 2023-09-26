"""

	Metadata:

		File: dbrouters.py
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

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class DBRouter:
	route_app_labels = {'dashboard'}

	def db_for_read(self, model, **hints):
		""" reading Model from default """
		if model._meta.app_label in self.route_app_labels:
			return getattr(model, "_database", "default")
		return None

	def db_for_write(self, model, **hints):
		""" writing Model to default """
		if model._meta.app_label in self.route_app_labels:
			return getattr(model, "_database", "default")
		return None

	def allow_relation(self, obj1, obj2, **hints):
		return True
