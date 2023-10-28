"""
	This module passes the controller logic off to a Svelte frontend.

	Metadata:

		File: frontend.py
		Project: emtautomation
		Created Date: 28 Oct 2022
		Author: Jess Mann
		Email: jmann@osc.ny.gov

		-----

		Last Modified: Sat Oct 28 2022
		Modified By: Jess Mann

		-----

		Copyright (c) 2022 NYSLRS
"""
# Generic imports
from __future__ import annotations
from djangofoundry.controllers import ListController

class SvelteController(ListController):
	"""
	This controller passes routing responsibility off to the Svelte frontend.
	"""
	# The template that contains our Svelte js code.
	template_name = 'dashboard/svelte.html'

	def get_queryset(self):
		"""
		Do not make any queries or return any data. Svelte will connect via our REST API to get the data it needs.

		Returns:
			An empty object.
		"""
		return {}