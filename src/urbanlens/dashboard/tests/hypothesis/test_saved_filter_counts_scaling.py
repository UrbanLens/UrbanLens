"""The saved-filter count badges must not read every pin to draw a number.

`SavedFilterMatchCountsView.get` (`controllers/saved_filters.py:314`) answers with
one count per saved filter — a body whose size is set by how many filters the
profile has, and which does not grow with the pin table at all. To build it, it
runs:

```python
base_uuids = {str(u) for u in base_query.values_list("uuid", flat=True)}
```

which pulls **every root pin's uuid** into Python on every request. The map page
calls this on each filter change.

That combination is invisible to every instrument the repo had before
`EndpointScalingMixin`:

- `QueryScalingMixin` sees one statement at every size, because there *is* one
  statement — perfectly flat, and correctly so;
- a bytes-per-row budget sees a body that never moves, because it never does;
- `InstantiationScalingMixin` sees no model objects, because `values_list` builds
  none. The projection is the right call; the unbounded read is not.

The axis that catches it is rows-fetched-per-row under the capped budget: an
endpoint whose *output* is bounded must have bounded *reading* behind it.

**These fail today, deliberately**, under `xfail(strict=True)` — they turn red
when the endpoint learns to count in the database (a `COUNT(*)` per filter, or
one grouped query) instead of in Python, which is the prompt to drop the markers.

The non-xfail companions below are what keep this honest, and both of them caught
something on their first run. The view returns early with an empty body when the
profile has no saved filters, so a version of this file that forgot to seed one
would measure a view that does nothing and call it bounded. And a profile arrives
with default saved filters of its own, so "the response carries one count" was
wrong — it carries three, and the assertion had to be about *this* filter's count
rather than how many came back.

The body is also not byte-identical across sizes, which is worth knowing before
writing `expect_growth=False` anywhere: the counts are rendered as digits, so ten
more pins add a character. The waiver's claim is that the body does not grow *with
the pin table*, and the honest way to assert that is against the harness's own
noise floor rather than against equality.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.endpoint_scaling import EndpointScalingMixin
from urbanlens.core.tests.scaling import MIN_GROWTH_BYTES
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter

_WAIVER = "the body carries one count per saved filter, so it does not grow with the pin table"


class _CountsCase(EndpointScalingMixin, TestCase):
    """A profile with one saved filter and a growing number of pins."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)
        self._seeded = 0
        # Without at least one, the view returns {"counts": {}} before touching
        # a pin, and every measurement below would be of an early return.
        self.saved_filter = baker.make(SavedFilter, profile=self.profile, criteria={})
        self.url = reverse("saved_filters.counts")

    def seed_rows(self, count: int) -> None:
        """Create *count* more root pins for this profile."""
        for _ in range(count):
            self._seeded += 1
            # Locations are unique on (latitude, longitude).
            location = baker.make(
                Location,
                latitude=f"44.{self._seeded:06d}",
                longitude=f"-70.{self._seeded:06d}",
                official_name="Counted Place",
            )
            baker.make(Pin, profile=self.profile, location=location)

    def count_payload_rows(self, response: HttpResponse) -> int | None:
        """How many counts came back — one per saved filter, never per pin."""
        payload = json.loads(response.content)
        counts = payload.get("counts")
        return len(counts) if isinstance(counts, dict) else None


class TheCountBadgesReadEveryPinTests(_CountsCase):
    """The reproduction: bounded output, unbounded reading."""

    @pytest.mark.xfail(strict=True, reason="SavedFilterMatchCountsView materialises every root pin uuid per request")
    def test_it_does_not_read_a_row_per_pin_to_answer(self) -> None:
        self.assert_endpoint_scaling(self.url, expect_growth=False, growth_waiver=_WAIVER)


class TheMeasurementIsRealTests(_CountsCase):
    """Guards the reproduction. Not xfail: if these fail, the one above is noise."""

    def test_the_endpoint_really_counts_the_seeded_pins(self) -> None:
        """Proves the view got past its early return and actually did the work.

        Asserted on this profile's own filter rather than on the number of
        filters: a profile is created with default saved filters of its own, so
        the response carries three counts here, not one. Their presence is not
        the point — the point is that the filter seeded with empty criteria
        matches every pin, so its count is the seeded row count and the view
        demonstrably reached the pin table.
        """
        seeded = 4
        self.seed(seeded)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        counts = json.loads(response.content)["counts"]
        self.assertEqual(
            counts.get(str(self.saved_filter.uuid)),
            seeded,
            f"the match-all filter counted {counts.get(str(self.saved_filter.uuid))!r} of {seeded} "
            f"seeded pins (full response {counts!r}) - the view returned early or filtered them "
            "out, and the scaling assertion above would be measuring nothing",
        )

    def test_the_body_does_not_grow_with_the_pin_table(self) -> None:
        """The premise of `expect_growth=False`, asserted rather than assumed.

        Not byte-identical: the counts themselves are rendered as digits, so ten
        more pins can add a character or two. That is not growth in the sense the
        waiver claims — the assertion is against the harness's own noise floor,
        which is the same threshold `assert_seed_exercised_endpoint` uses to
        decide a seed did nothing.
        """
        self.seed(2)
        small = len(self.client.get(self.url).content)
        self.seed(10)
        large = len(self.client.get(self.url).content)

        self.assertLess(
            large - small,
            MIN_GROWTH_BYTES,
            f"the body moved from {small} to {large} bytes for ten more pins, so the growth waiver "
            "is wrong and the capped rows-fetched budget is being applied to an endpoint that does grow",
        )

    def test_the_query_count_stays_flat(self) -> None:
        """Proves the new axis is necessary: a statement counter calls this fine."""
        self.seed(2)
        small = self.measure_queries(self.url)
        self.seed(10)
        large = self.measure_queries(self.url)

        self.assertLessEqual(
            len(large),
            len(small) + 2,
            "the query count grew, so QueryScalingMixin would already have caught this and the "
            "rows-fetched axis is not what this endpoint demonstrates",
        )
