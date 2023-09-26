"""

	Metadata:

		File: home.py
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
from djangofoundry.controllers import DetailController

class IndexController(DetailController):
	'''
	Controller for the home page.
	'''
	def __init__(self):
		pass

	def get(self):
		'''
		GET request handler for the home page.
		'''
		return 'Hello World'