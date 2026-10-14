"""
Every seam core offers plugins.

Core reads these where the behaviour lives; plugins contribute to them from ``setup()``. Each area's seams are
declared in their own module and re-exported here, so a plugin can write ``points.PANEL_SOURCES``. Interface types are
imported only for type checking, so importing a seam never imports the subsystem that reads it.

Lower priorities come first. Provider chains are asked in priority order, so a plugin that should be preferred over
the bundled public providers registers below their default of 100.
"""

from urbanlens.dashboard.plugins.points.accounts import *  # noqa: F403
from urbanlens.dashboard.plugins.points.admin import *  # noqa: F403
from urbanlens.dashboard.plugins.points.contributions import *  # noqa: F403
from urbanlens.dashboard.plugins.points.demo import *  # noqa: F403
from urbanlens.dashboard.plugins.points.facts import *  # noqa: F403
from urbanlens.dashboard.plugins.points.geo import *  # noqa: F403
from urbanlens.dashboard.plugins.points.imports import *  # noqa: F403
from urbanlens.dashboard.plugins.points.labels import *  # noqa: F403
from urbanlens.dashboard.plugins.points.lifecycle import *  # noqa: F403
from urbanlens.dashboard.plugins.points.maps import *  # noqa: F403
from urbanlens.dashboard.plugins.points.media import *  # noqa: F403
from urbanlens.dashboard.plugins.points.panels import *  # noqa: F403
from urbanlens.dashboard.plugins.points.pins import *  # noqa: F403
from urbanlens.dashboard.plugins.points.places import *  # noqa: F403
from urbanlens.dashboard.plugins.points.providers import *  # noqa: F403
from urbanlens.dashboard.plugins.points.services import *  # noqa: F403
from urbanlens.dashboard.plugins.points.slots import *  # noqa: F403
from urbanlens.dashboard.plugins.points.tasks import *  # noqa: F403
from urbanlens.dashboard.plugins.points.trivia import *  # noqa: F403
from urbanlens.dashboard.plugins.points.web import *  # noqa: F403
from urbanlens.dashboard.plugins.points.wiki import *  # noqa: F403
from urbanlens.dashboard.plugins.points.wikis import *  # noqa: F403
