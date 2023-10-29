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
from django.utils.translation import gettext as _
from djangofoundry.models.choices import TextChoices

class NotificationType(TextChoices):
	"""
	Choices used for recording the status of a notification.

	This is used as a class, and never instantiated.
	"""
	ERROR	= 'error'
	WARNING = 'warning'
	INFO	= 'info'