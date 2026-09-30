"""0101 moves wiki boundary edits from inline WKT to revision ids, and its reverse must restore the WKT.

The pre-migration code reads ``WikiEdit.changes["boundary_<type>"]`` as WKT, so a no-op reverse would leave it
reading integers.
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.gis.geos import GEOSGeometry
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")
_OLD = "MULTIPOLYGON (((-73.75 42.65, -73.749 42.65, -73.749 42.651, -73.75 42.651, -73.75 42.65)))"
_NEW = "MULTIPOLYGON (((-73.76 42.66, -73.759 42.66, -73.759 42.661, -73.76 42.661, -73.76 42.66)))"


class BoundaryRevisionMigrationReverseTests(TestCase):
    def test_a_reverse_restores_the_inline_outlines(self) -> None:
        edit = baker.make(
            WikiEdit,
            wiki=baker.make(Wiki),
            changes={"boundary_property": {"from": _OLD, "to": _NEW}, "name": {"from": "a", "to": "b"}},
        )

        _MIGRATION._0101_convert_inline_boundaries(apps, None)
        edit.refresh_from_db()
        self.assertIsInstance(edit.changes["boundary_property"]["to"], int)

        _MIGRATION._0101_restore_inline_boundaries(apps, None)
        edit.refresh_from_db()
        restored = edit.changes["boundary_property"]
        self.assertTrue(GEOSGeometry(restored["from"]).equals_exact(GEOSGeometry(_OLD), 1e-9))
        self.assertTrue(GEOSGeometry(restored["to"]).equals_exact(GEOSGeometry(_NEW), 1e-9))
        self.assertEqual(edit.changes["name"], {"from": "a", "to": "b"})
