"""

	Metadata:

		File: types.py
		Project: Urban Lens
		
		Author: Jess Mann
		Email: jess@manlyphotos.com

		-----

		
		Modified By: Jess Mann

		-----

		Copyright (c) 2022 UrbanLens
"""
# Generic imports
from __future__ import annotations
from enum import Enum
from typing import Any, TypedDict

class LoggerLevels(Enum):
	DEBUG = 'debug'
	INFO = 'info'
	ERROR = 'error'
	WARNING = 'warning'
	WARN = WARNING

class YesNo(Enum):
	YES = 'yes'
	Y = YES
	NO = 'no'
	N = NO

class Logger(TypedDict):
	"""
	Expected format for a "logger" in the settings file.
	"""
	level: LoggerLevels
	handlers: list[str]
	propogate: YesNo

class LogFormatter(TypedDict):
	format: str

class LogHandler(TypedDict):
	level: LoggerLevels
	formatter: str
	stream: str
	#class: str

class LogRoot(TypedDict):
	level: LoggerLevels
	handlers: list[LogHandler]

class SettingsLog(TypedDict):
	"""
	Expected format for the logging portion of the settings file.

	This is useful to provide type hints in our editor.
	"""
	version: int
	formatters: dict[str, LogFormatter]
	handlers: dict[str, LogHandler]
	loggers: dict[str, Logger]
	root: LogRoot

class BrowserSync(TypedDict):
	startPath: str
	watch: list[str]
	proxy: str
	reload_delay: int
	reload_debounce: int

class SettingsFile(TypedDict):
	"""
	Expected format of the settings file.

	This is useful to provide type hints in our editor.
	"""
	version: int
	logging: SettingsLog
	browsersync: BrowserSync