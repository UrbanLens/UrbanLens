"""The export must not issue a query per exported row.

Two sites in ``services/import_export/export.py`` prefetched a relation and then
read it with a verb that bypasses the prefetch cache, so each one paid for the
fetch *and* queried per row anyway - over a whole account, which is where an
export's row counts come from.

Both were fixed on 2026-08-14 (commits ``4dc6b596``, ``f7cc04d3``) and both were
silently discarded five days later when merge ``3fcd6ab3`` resolved this file in
favour of the release branch. Neither commit carried a test, which is the only
reason the regression survived the merge unnoticed; this file is that test.
"""

from __future__ import annotations

import tempfile

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.query_scaling import queries_that_grew
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.import_export.export import _export_direct_messages, _export_labels

#: Rows seeded before each measurement. The second is far enough above the first
#: that one query per row cannot hide inside normal variation.
_FIRST = 2
_SECOND = 10

#: A second measurement may legitimately differ by a query or two; more than
#: that is slope, not noise.
_TOLERANCE = 2


class ExportQueryScalingTests(TestCase):
    """Exporting ten times the rows must not cost ten times the queries."""

    def setUp(self):
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.other = baker.make("auth.User").profile
        # Each measurement seeds on top of the last, so names must not repeat -
        # labels are unique per (lower(name), profile, kind).
        self.seeded = 0

    def _measure(self, export, profile) -> list[dict]:
        with tempfile.TemporaryDirectory() as temp_dir, CaptureQueriesContext(connection) as captured:
            export(profile, temp_dir)
        return list(captured.captured_queries)

    def _assert_flat(self, export, seed, profile) -> None:
        seed(_FIRST)
        small = self._measure(export, profile)
        seed(_SECOND)
        large = self._measure(export, profile)

        if len(large) > len(small) + _TOLERANCE:
            grew = queries_that_grew(small, large)[:3]
            detail = "\n".join(f"  {before} -> {after}: {sql}" for before, after, sql in grew)
            self.fail(f"{export.__name__} ran {len(small)} queries for {_FIRST} rows and {len(large)} for {_FIRST + _SECOND}.\nWhat multiplied:\n{detail}")

    def _seed_labels(self, count: int) -> None:
        for _ in range(count):
            self.seeded += 1
            label = Label.objects.create(profile=self.profile, name=f"label-{self.seeded}")
            label.pins.add(baker.make(Pin, profile=self.profile))

    def _seed_messages(self, count: int) -> None:
        for _ in range(count):
            self.seeded += 1
            baker.make(DirectMessage, sender=self.profile, recipient=self.other, body=f"body {self.seeded}")

    def test_label_export_does_not_query_per_label(self):
        """`label.pins.filter(...)` on a prefetched relation is a query per label."""
        self._assert_flat(_export_labels, self._seed_labels, self.profile)

    def test_message_export_does_not_query_per_message(self):
        """`message.images.count()` on a prefetched relation is a query per message."""
        self._assert_flat(_export_direct_messages, self._seed_messages, self.profile)

    def test_a_global_label_exports_only_the_owner_s_own_pins(self):
        """The narrowed prefetch must not change what the export contains.

        A global label is visible to everyone, so prefetching its whole `pins`
        relation pulls other profiles' pins. Narrowing that fetch is only safe
        if the exported list still holds exactly the exporter's own pins.
        """
        import json
        import os

        shared = Label.objects.create(profile=None, name="global-label")
        mine = baker.make(Pin, profile=self.profile)
        theirs = baker.make(Pin, profile=self.other)
        shared.pins.add(mine, theirs)

        with tempfile.TemporaryDirectory() as temp_dir:
            _export_labels(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "labels.json"), encoding="utf-8") as fh:
                rows = json.load(fh)

        exported = next(row for row in rows if row["name"] == shared.name)
        self.assertEqual(exported["pin_uuids"], [str(mine.uuid)], "a global label must export the exporter's pins and nobody else's")
