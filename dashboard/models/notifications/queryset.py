"""

	Metadata:

		File: queryset.py
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
# Django Imports
# Lib Imports
# App Imports
from dashboard.models import abstract
from djangofoundry.helpers.queue import Queue as BaseQueue

class Queue(BaseQueue):
	'''
	A custom model queue for bulk inserting this module's models into the local postrgres db
	'''
	unique_key = ['id']

class QuerySet(abstract.QuerySet):
	"""
	A queryset for interacting with our local DB.
	"""

class Manager(abstract.Manager.from_queryset(QuerySet)):
	"""
	A manager for creating querysets.

	This class inherits the methods from QuerySet in this module (although VSCode doesn't show them as hints)
	"""