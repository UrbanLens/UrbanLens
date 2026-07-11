"""Bundled UrbanLens plugins.

Every module in this package is imported and scanned for
:class:`~urbanlens.dashboard.plugins.base.UrbanLensPlugin` subclasses during
plugin discovery. Adding a new bundled integration means dropping a module
here; removing one means deleting its module (or listing its plugin name in
the ``UL_DISABLED_PLUGINS`` setting to disable it for one install).
"""
