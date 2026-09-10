"""The child-pins layer still builds a model graph per row, as the map once did.

R27 moved the main map's payload off model instances and onto a column
projection — 10,000 pins went from 63,240 objects and 5.8s to zero objects and
~0.33s. `map_child_pins_json` (`controllers/maps.py:702-748`) was not moved with
it, and still does the thing that was fixed:

```python
query = (... .select_related("location", "parent_pin", "parent_pin__location")
             .prefetch_related(Prefetch("labels", ...)))
for child in query:
    entry = child.to_detail_json()
```

so every child pin costs its own `Pin`, its `Location`, its parent `Pin`, that
parent's `Location`, and a fresh `Label` per pin-label pair — the exact fan-out
R27 measured, on an endpoint the map's "Child pins" layer calls directly.

It is smaller than the main map was, because most accounts have far fewer child
pins than root pins. That is a reason it has not hurt yet, not a reason the shape
is right: nothing bounds how many child pins a profile owns, and D12 puts this
endpoint on the projection path with the rest.

**Fails today, deliberately**, under `xfail(strict=True)`.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.endpoint_scaling import EndpointScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class _ChildPinsCase(EndpointScalingMixin, TestCase):
    """A profile with one parent pin and a growing number of children under it."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        self._seeded = 0
        self.parent = self._pin()
        self.url = reverse("map.pins.children")

    def _pin(self, parent: Pin | None = None) -> Pin:
        """One pin with a location of its own, optionally nested under *parent*."""
        self._seeded += 1
        # Locations are unique on (latitude, longitude).
        location = baker.make(
            Location,
            latitude=f"42.{self._seeded:06d}",
            longitude=f"-71.{self._seeded:06d}",
            official_name="Child Place",
        )
        return baker.make(Pin, profile=self.profile, location=location, parent_pin=parent)

    def seed_rows(self, count: int) -> None:
        """Create *count* more child pins under the shared parent."""
        for _ in range(count):
            self._pin(parent=self.parent)

    def count_payload_rows(self, response: HttpResponse) -> int | None:
        """How many child pins the layer shipped."""
        payload = json.loads(response.content)
        pins = payload.get("pins")
        return len(pins) if isinstance(pins, list) else None


class TheChildLayerBuildsAGraphPerRowTests(_ChildPinsCase):
    """One child pin should not cost a pin, a location, a parent and a parent's location."""

    @pytest.mark.xfail(
        strict=True,
        reason="map_child_pins_json is still on the model path; D12 moves it to the projection",
    )
    def test_one_more_child_pin_does_not_cost_a_model_graph(self) -> None:
        self.assert_endpoint_scaling(self.url)


class TheMeasurementIsRealTests(_ChildPinsCase):
    """Guards the reproduction. Not xfail: if these fail, the one above is noise."""

    def test_the_layer_returns_the_seeded_children(self) -> None:
        """Proves the endpoint reached the child pins rather than answering empty."""
        seeded = 5
        self.seed(seeded)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        pins = json.loads(response.content)["pins"]
        self.assertEqual(
            len(pins),
            seeded,
            f"the layer returned {len(pins)} pins for {seeded} seeded children - the scaling "
            "assertion above would be measuring an endpoint that found nothing",
        )

    def test_the_parent_is_not_itself_in_the_layer(self) -> None:
        """`detail_pins()` means children only, which is what makes the count exact."""
        self.seed(3)

        pins = json.loads(self.client.get(self.url).content)["pins"]

        uuids = {pin.get("uuid") for pin in pins}
        self.assertNotIn(
            str(self.parent.uuid),
            uuids,
            "the parent pin came back in the child layer, so the seeded row count is not the "
            "number of rows the endpoint renders",
        )
