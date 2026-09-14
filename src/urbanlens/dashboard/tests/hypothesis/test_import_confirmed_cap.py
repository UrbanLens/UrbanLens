"""The confirm step refuses an oversized selection, and never imports on the request.

`GoogleMapsGateway.MAX_PREVIEW_PINS` bounds the preview, but the confirm step gets
the selection back from the client, so nothing requires it to be one the server
previewed, or any particular size. Two claims, because a partial fix should not
look complete:

- a payload above the cap is refused outright, rather than attempted;
- a payload below the cap does its work in a task, so the request returns without
  having created the pins itself.

The payloads are ones the importer really acts on. An earlier draft sent keys the
generator ignored, created nothing, and passed every "created no pins" assertion
while proving nothing - see `_pin_payload`.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

#: Stand-in for the real 20,000 cap, so the test states the rule without building
#: a 20,001-element payload. Patched onto the gateway the view reads it from.
TEST_CAP = 5


def _pin_payload(index: int) -> dict[str, Any]:
    """One confirmed pin, in the shape the preview step hands the client.

    Keys are `iter_confirmed_import_events`'s documented contract (`name`, `lat`,
    `lng`, `description`, `cid`, `maps_url`, `label_ids`), and getting them
    wrong is not a harmless mismatch: a payload the generator does not recognise
    imports nothing, so an "it created no pins" assertion over it passes without
    the endpoint having done anything. Deliberately no `cid` - a pin carrying one
    with no matching Location is queued for background resolution rather than
    placed, which would produce the same vacuous pass.
    """
    return {
        "name": f"Imported {index}",
        # Spread by whole degrees, not fractions. At 0.0001 degrees apart (~11m)
        # the importer matched every pin to the first one's Location and reported
        # "exists" for the rest, so three confirmed pins created one row - which
        # would understate the work the endpoint does and weaken every assertion
        # here. Verified against the live stream: widely-spaced pins each create.
        "lat": 30.0 + index * 0.5,
        "lng": -120.0 + index * 0.5,
        "description": "",
        "cid": None,
        "maps_url": "",
        "label_ids": [],
    }


def _lists(count: int) -> list[dict[str, Any]]:
    """A single confirmed list carrying *count* pins."""
    return [
        {
            "stem": "probe",
            "create_category": False,
            "label_ids": [],
            "pins": [_pin_payload(index) for index in range(count)],
        },
    ]


class ImportConfirmedRefusesAnOversizePayloadTests(TestCase):
    """The confirm step must apply the same ceiling the preview step does."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("pin.import.confirmed")

    def _post(self, lists: list[dict[str, Any]]) -> Any:
        return self.client.post(
            self.url,
            data=json.dumps({"lists": lists, "auto_tag": False}),
            content_type="application/json",
        )

    def test_a_payload_over_the_cap_is_refused(self) -> None:
        with mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", TEST_CAP):
            response = self._post(_lists(TEST_CAP + 1))

        self.assertEqual(
            response.status_code,
            400,
            f"the confirm step accepted {TEST_CAP + 1} pins against a cap of {TEST_CAP} "
            "(status %s). MAX_PREVIEW_PINS is enforced on preview only - P96." % response.status_code,
        )

    def test_nothing_is_created_when_the_payload_is_refused(self) -> None:
        """A refusal must happen before the work, not partway through it."""
        with mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", TEST_CAP):
            self._post(_lists(TEST_CAP + 1))

        self.assertEqual(
            Pin.objects.filter(profile=self.user.profile).count(),
            0,
            "the over-cap import created pins anyway",
        )

    def test_the_cap_counts_pins_across_every_list_not_per_list(self) -> None:
        """Splitting the same pins across lists must not multiply the ceiling."""
        half = TEST_CAP // 2 + 1
        lists = [
            {"stem": "a", "create_category": False, "label_ids": [], "pins": [_pin_payload(i) for i in range(half)]},
            {
                "stem": "b",
                "create_category": False,
                "label_ids": [],
                "pins": [_pin_payload(i + half) for i in range(half)],
            },
        ]

        with mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", TEST_CAP):
            response = self._post(lists)

        self.assertEqual(
            response.status_code,
            400,
            f"{2 * half} pins split across two lists passed a cap of {TEST_CAP}",
        )


class ImportConfirmedDoesTheWorkOffTheRequestTests(TestCase):
    """Even a payload inside the cap must not be imported on the web worker."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("pin.import.confirmed")

    def test_an_accepted_import_creates_no_pins_inside_the_request(self) -> None:
        """The request should hand the work to a task and return.

        Asserted on the row count rather than on which function was called, so it stays true whichever task ends
        up doing the work - the claim is about where the CPU is spent."""
        with (
            mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", TEST_CAP),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", return_value=mock.Mock()),
        ):
            response = self.client.post(
                self.url,
                data=json.dumps({"lists": _lists(TEST_CAP - 1), "auto_tag": False}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202, "the import was refused, so its pin count proves nothing")
        self.assertEqual(
            Pin.objects.filter(profile=self.user.profile).count(),
            0,
            "import_confirmed created pins on the request path; the import belongs in a task "
            "(D11's bulk queue), so one user's 20,000-pin import cannot hold a worker.",
        )
