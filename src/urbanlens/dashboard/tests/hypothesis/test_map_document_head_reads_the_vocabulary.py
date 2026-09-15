"""The map document defines its labels from the account's vocabulary, not by walking every label on every pin.

The head's dictionary was the distinct labels on the account's pins. For the capacity population's heaviest account that
read all 53,160 pin-label pairs, 113-171 ms of every document build, to name 17 of the 63 labels it can see.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import document as map_document
from urbanlens.dashboard.services.map_pins.payload import MapPinPayloadService

MAP_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "dashboard" / "pages" / "map" / "index.html"

MORE_PINS = 25


class _HeadWrittenError(Exception):
    """Raised where the document would read its first batch of pins."""


def _scanned(node: dict) -> int:
    own = 0
    if "Relation Name" in node:
        own = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * node.get("Actual Loops", 1)
    return own + sum(_scanned(child) for child in node.get("Plans", []))


def _rows_examined(sql: str, params: object) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", params)
        row = cursor.fetchone()
    raw = row[0] if row else "[]"
    plan = raw if isinstance(raw, list) else json.loads(raw)
    return _scanned(plan[0]["Plan"])


class TheDocumentHeadTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.labels = [
            baker.make(Label, profile=self.profile, kind="tag", name="Mine", order=3),
            baker.make(Label, profile=None, kind="category", name="Everyone's", order=2),
            baker.make(Label, profile=self.profile, kind="status", name="Standing", order=1),
        ]
        self.placed = 0
        self._pin_with(self.labels, count=4)

    def _pin_with(self, labels: list[Label], count: int = 1) -> None:
        for _ in range(count):
            self.placed += 1
            location = baker.make(Location, latitude=10 + self.placed * 0.01, longitude=20.0)
            baker.make(Pin, profile=self.profile, location=location).labels.set(labels)

    def _lines(self) -> list[dict]:
        response = self.client.get(reverse("map.document"))
        body = b"".join(response.streaming_content) if response.streaming else response.content
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return [json.loads(line) for line in body.splitlines() if line]

    def _head_rows_examined(self) -> int:
        captured: list[tuple[str, object]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        query = Pin.objects.filter(profile=self.profile)
        with (
            mock.patch.object(MapPinPayloadService, "page", side_effect=_HeadWrittenError),
            connection.execute_wrapper(capture),
            self.assertRaises(_HeadWrittenError),
        ):
            next(map_document.stream(self.profile, query, etag="head", total=0))
        self.assertTrue(captured, "the head read nothing, so there was nothing to measure")
        return sum(_rows_examined(sql, params) for sql, params in captured)

    def test_the_head_reads_no_more_rows_for_more_labelled_pins(self) -> None:
        before = self._head_rows_examined()

        self._pin_with(self.labels, count=MORE_PINS)

        self.assertEqual(self._head_rows_examined(), before)

    def test_every_label_is_defined_before_the_pin_that_names_it(self) -> None:
        carried_over = baker.make(Label, profile=baker.make(User).profile, kind="tag", name="Carried over", order=5)
        self._pin_with([*self.labels, carried_over])

        defined: set[str] = set()
        named = 0
        for line in self._lines():
            defined.update(line.get("labels") or {})
            if line["t"] == "pin":
                ids = {str(label_id) for label_id in line["p"]["label_ids"]}
                self.assertEqual(ids - defined, set(), "a pin names a label the document has not defined yet")
                named += len(ids)
        self.assertGreater(named, 0, "no pin named a label, so nothing was checked")
        self.assertIn(str(carried_over.pk), defined)

    def test_the_head_carries_this_accounts_customization(self) -> None:
        baker.make(LabelCustomization, profile=self.profile, label=self.labels[1], color="#123456")

        head = self._lines()[0]

        self.assertEqual(head["labels"][str(self.labels[1].pk)]["color"], "#123456")

    def test_the_head_leaves_out_people_labels_and_other_accounts_labels(self) -> None:
        person = baker.make(Label, profile=self.profile, kind="user", name="Friend")
        theirs = baker.make(Label, profile=baker.make(User).profile, kind="tag", name="Theirs")

        head = self._lines()[0]

        self.assertIn(str(self.labels[0].pk), head["labels"])
        self.assertNotIn(str(person.pk), head["labels"])
        self.assertNotIn(str(theirs.pk), head["labels"])

    def test_the_map_page_reads_every_kind_of_line_the_document_sends(self) -> None:
        self._pin_with([baker.make(Label, profile=baker.make(User).profile, kind="tag", name="Carried over")])

        sent = {line["t"] for line in self._lines()}
        read = set(re.findall(r"obj\.t === '(\w+)'", MAP_TEMPLATE.read_text()))

        self.assertEqual(sent - read, set())
