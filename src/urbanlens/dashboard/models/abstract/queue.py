"""

	Metadata:

		File: queue.py
		Project: UrbanLens

		Author: Jess Mann
		Email: jess@urbanlens.org

		-----


		Modified By: Jess Mann

		-----

		Copyright (c) 2023 UrbanLens
"""
# Generic imports
from __future__ import annotations
import logging
# App imports
from djangofoundry.helpers import queue
#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)

class Queue(queue.Queue):
	unique_key = ['id']
