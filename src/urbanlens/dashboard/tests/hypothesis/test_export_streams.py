"""The data export writes rows as it reads them rather than building each file in memory."""

from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from hypothesis import example, given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.import_export import export
from urbanlens.dashboard.services.import_export.export import JsonArrayFile

_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3),
    max_leaves=10,
)


class JsonArrayFileTests(SimpleTestCase):
    @settings(max_examples=60, deadline=None)
    @given(st.lists(_json, max_size=5))
    @example([{"": "line separator paragraph"}])
    def test_writes_exactly_what_json_dump_writes(self, rows: list) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            streamed_path = os.path.join(temp_dir, "streamed.json")
            with JsonArrayFile(streamed_path) as writer:
                for row in rows:
                    writer.append(row)
            with open(streamed_path, encoding="utf-8") as fh:
                streamed = fh.read()

        self.assertEqual(streamed, json.dumps(rows, indent=2, ensure_ascii=False))


class JsonArrayFileInterruptedTests(SimpleTestCase):
    def test_an_interrupted_array_is_removed_rather_than_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "rows.json")

            def interrupted_export() -> None:
                with JsonArrayFile(path) as writer:
                    writer.append({"a": 1})
                    raise RuntimeError("row two failed")

            self.assertRaises(RuntimeError, interrupted_export)
            self.assertFalse(os.path.exists(path))


class ExportReadsInChunksTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        label = Label.objects.create(profile=self.profile, name="Tag")
        self.pins: list[Pin] = []
        for index in range(5):
            pin = baker.make(Pin, profile=self.profile, name=f"Pin {index}")
            pin.labels.add(label)
            self.pins.append(pin)

    def test_pins_are_fetched_a_chunk_at_a_time_and_written_in_order(self) -> None:
        with (
            mock.patch.object(export, "EXPORT_CHUNK_SIZE", 2),
            tempfile.TemporaryDirectory() as temp_dir,
            CaptureQueriesContext(connection) as ctx,
        ):
            export._export_pins(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "pins.json"), encoding="utf-8") as fh:
                rows = json.load(fh)

        label_prefetches = [
            q for q in ctx.captured_queries if '"dashboard_labels"' in q["sql"] and "INNER JOIN" in q["sql"]
        ]
        self.assertEqual(len(label_prefetches), 3, "one labels prefetch per chunk of two")
        self.assertEqual(
            [row["uuid"] for row in rows], [str(pin.uuid) for pin in sorted(self.pins, key=lambda p: (p.created, p.pk))]
        )
        self.assertTrue(all(len(row["label_uuids"]) == 1 for row in rows))

    def test_takeout_csv_is_written_row_by_row(self) -> None:
        with mock.patch.object(export, "EXPORT_CHUNK_SIZE", 2), tempfile.TemporaryDirectory() as temp_dir:
            export._export_pins_google_takeout(self.profile, temp_dir)
            with open(os.path.join(temp_dir, "google_takeout", "pins.csv"), encoding="utf-8") as fh:
                lines = fh.read().splitlines()

        self.assertEqual(lines[0], "Title,Note,URL,Tags,Comment")
        self.assertEqual(len(lines), 6)
