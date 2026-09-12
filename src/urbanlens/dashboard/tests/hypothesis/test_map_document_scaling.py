"""`map.document` is the largest response the application serves; gate it."""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import EndpointScalingMixin, read_body
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.map_pins import document as map_document

#: Response bytes one pin may add.
MAX_DOCUMENT_BYTES_PER_PIN = 900

#: Labels attached to each seeded pin. More than zero on purpose: `label_ids`
#: plus the per-response dictionary is the v11 shape, and a seed with no labels
#: would not exercise the part of the payload most likely to regress.
LABELS_PER_PIN = 2

#: Database rows one rendered pin may cost.
#: Derived rather than chosen: the pin's own projected row, plus one through-row per label it names, which is
#: what emitting `label_ids` legitimately costs.
MAX_ROWS_FETCHED_PER_PIN = 1.0 + LABELS_PER_PIN + 0.5


class MapDocumentScalingTests(EndpointScalingMixin, TestCase):
    """One more pin must cost one more line, and nothing else."""

    first_batch = 6
    second_batch = 12

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self._seeded = 0
        self.labels = [
            baker.make(Label, name=f"Document Gate {index}", kind="category") for index in range(LABELS_PER_PIN)
        ]

    def seed_rows(self, count: int) -> None:
        """Create *count* more root pins, each carrying the shared labels.

        Args:
            count: How many pins to add.
        """
        for _ in range(count):
            self._seeded += 1
            location = baker.make(
                Location,
                latitude=f"41.{self._seeded:06d}",
                longitude=f"-71.{self._seeded:06d}",
                official_name=f"Document Gate Place {self._seeded}",
            )
            pin = baker.make(Pin, profile=self.profile, location=location, name=f"Gate Pin {self._seeded}")
            pin.labels.set(self.labels)

    def count_payload_rows(self, response) -> int | None:  # noqa: ANN001
        """How many pin lines the document carries.

        Args:
            response: The streamed response.

        Returns:
            The pin-line count."""
        lines = [line for line in read_body(response).decode().splitlines() if line.strip()]
        return sum(1 for line in lines if json.loads(line).get("t") == "pin")

    def test_one_more_pin_costs_one_more_line_and_nothing_else(self) -> None:
        self.assert_endpoint_scaling(
            reverse("map.document"),
            max_objects_per_row=0.0,
            max_bytes_per_row=MAX_DOCUMENT_BYTES_PER_PIN,
            max_rows_fetched_per_row=MAX_ROWS_FETCHED_PER_PIN,
            payload_ceiling=map_document.max_pins(),
        )

    def test_the_document_really_carries_the_pins(self) -> None:
        """Without this the budgets above could be passing on an empty document."""
        self.seed(self.first_batch)
        rows = self.count_payload_rows(self.request(reverse("map.document")))
        self.assertEqual(
            rows,
            self.first_batch,
            f"the document carried {rows} pin lines for {self.first_batch} seeded pins, so every "
            "budget in this file would be measuring the wrong thing",
        )

    def test_the_labels_are_really_on_the_pins(self) -> None:
        """The v11 shape is `label_ids` plus one dictionary; seed both halves."""
        self.seed(self.first_batch)
        body = read_body(self.request(reverse("map.document"))).decode()
        head = json.loads(body.splitlines()[0])
        # A pin line carries its payload under "p"; the line itself is grammar.
        pin = next(json.loads(line)["p"] for line in body.splitlines() if json.loads(line).get("t") == "pin")
        self.assertEqual(len(pin["label_ids"]), LABELS_PER_PIN, "the seeded pins carry no labels")
        self.assertTrue(head.get("labels"), "the head defines no labels, so the dictionary is untested here")
