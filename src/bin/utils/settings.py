"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    settings.py                                                                                          *
*        Path:    /settings.py                                                                                         *
*        Project: utils                                                                                                *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
# Generic imports
from __future__ import annotations
import os
from typing import Any
import yaml
from yaml.loader import SafeLoader
import logging
import logging.config
# App imports
from .exceptions import FileEmptyError
from .types import SettingsFile, SettingsLog

SETTINGS_PATH : str = '../conf/settings.yaml'

# Add placeholders for new API keys and endpoints
INSTAGRAM_ACCESS_TOKEN = 'your-instagram-access-token-placeholder'
INSTAGRAM_GRAPH_URL = 'your-instagram-graph-url-placeholder'
GOOGLE_LENS_API_KEY = 'your-google-lens-api-key-placeholder'
GOOGLE_LENS_URL = 'your-google-lens-url-placeholder'

class Settings:
	"""
	Settings for our application (used in /bin files only).

	These are loaded from the file at SETTINGS_PATH (currently bin/conf/settings.yaml).
	"""
	_settings : SettingsFile | None = None
	_logging_setup : bool = False

	@classmethod
	@property
	def settings(cls) -> SettingsFile:
		# If settings has never been loaded, then load it.
		if cls._settings is None:
			cls.load_config()

		# It should exist now
		return cls._settings or {}

	@classmethod
	@property
	def logging(cls) -> SettingsLog:
		return cls.settings.get('logging')

	@classmethod
	def getLogger(cls, namespace : str):
		"""
		Sets up the logger once (and only once), then returns a logger for the module requested.
		"""
		# Setup logging if it isn't already
		if cls._logging_setup is not True:
			try:
				logging.config.dictConfig(Settings.logging)
			except Exception as e:
				print(f'Unable to set up logging: {e}')
				raise e from e
			cls._logging_setup = True

		# Create a new logger
		return logging.getLogger(namespace)

	@classmethod
	def load_config(cls) -> SettingsFile:
		# Read our default sensitivity settings (if available)
		filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_PATH)

		if (os.path.exists(filepath)):
			# If it exists, then open it
			with open(filepath) as file:
				# Load the contents into a variable
				cls._settings = yaml.load(file, Loader=SafeLoader)
		else:
			# Let everyone know we couldn't find the settings. This likely exits.
			raise FileNotFoundError(f"Could not load bin settings from {filepath}")

		# Validate contents of settings file.
		if cls._settings == {}:
			raise FileEmptyError(f'No data in settings file at f{filepath}')

		return cls._settings

	@classmethod
	def all(cls) -> SettingsFile:
		"""
		Makes the syntax for getting the settings dict a little less clunky (i.e. Settings.all() instead of Settings.settings)

		Returns:
			dict: A dictionary of settings.
		"""
		return cls.settings

	@classmethod
	def get(cls, key : str) -> Any:
		"""
		Retrieves the value at the provided key.

		Args:
			key (str): A key to retrieve

		Returns:
			Any: The value stored at the provided key
		"""
		return cls.settings.get(key)

if __name__ == '__main__':
	conf = Settings.settings
	print(conf)
