"""

	Metadata:

		File: status.py
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
from djangofoundry.models.choices import TextChoices

class Importance(TextChoices):
	"""
	Choices used for recording the status of a notification.

	This is used as a class, and never instantiated.
	"""
	LOWEST		= 'lowest'
	LOW		 = 'low',
	MEDIUM	  = 'medium',
	HIGH		= 'high',
	CRITICAL	= 'critical',