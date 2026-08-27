"""How many requests the pin detail page fires the moment it opens.

The integration suite found this page exhausting the database connection pool:
it loads its enrichment panels with `hx-trigger="load"`, so opening it issues
roughly thirty requests at once, each of which is a Django request taking its own
connection (`CONN_MAX_AGE` is 0). Against a Postgres with the default
`max_connections`, a few simultaneous readers is enough to start answering 500s
from whichever panel arrives when the pool is full.

**This test cannot reproduce that**, and it is worth being clear about the
limits rather than implying otherwise. Exhaustion needs concurrency against a
real pool, which does not exist in a suite that issues one request at a time -
see `docs/TEST_COVERAGE_GAPS.md`, where the pool itself is listed as
integration-only. What a unit test *can* hold is the number, which is the thing
that causes it, and which creeps up one innocuous panel at a time.

So this is a ratchet rather than an assertion of correctness. The current count
is already too high; the budget stops it growing while the real fix - loading
panels in waves, or behind one request - is decided. Lower the ceiling when that
happens. Raising it should take an argument.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

#: The most load-triggered HTMX requests the pin detail page may fire.
#:
#: Set to the count at the time of writing - **53** - which is a ratchet, not an
#: endorsement. That number is already more than the dev deployment's Postgres
#: could serve concurrently; the budget exists to stop it growing while the real
#: fix is decided, not to say the current value is fine.
#:
#: Not all 53 fire on every load: some carry a filter
#: (`load[!window.ulSectionCollapsed(...)]`) and stay quiet for a collapsed
#: section, which is why the deployment showed around thirty concurrent requests
#: rather than 53. The static count is still the right thing to bound, because
#: it is the ceiling a user with everything expanded actually reaches.
MAX_LOAD_TRIGGERED_REQUESTS = 53

#: An element that fetches as soon as the page loads. `load` may carry a filter
#: (`load[!window.ulSectionCollapsed(...)]`) or sit alongside other triggers, so
#: the match is on the word rather than the whole attribute.
_LOAD_TRIGGER = re.compile(r'hx-trigger="[^"]*\bload\b[^"]*"')


class PinDetailFanoutBudgetTests(TestCase):
    """The page's opening burst has a ceiling."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), parent_pin=None)
        self.client.force_login(self.user)

    def _rendered(self) -> str:
        response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200, "the pin detail page did not render, so nothing was counted")
        return response.content.decode()

    def test_the_page_does_not_fire_more_requests_on_load_than_its_budget(self) -> None:
        html = self._rendered()
        count = len(_LOAD_TRIGGER.findall(html))

        self.assertLessEqual(
            count,
            MAX_LOAD_TRIGGERED_REQUESTS,
            f"the pin detail page now fires {count} requests the moment it opens, over its budget of "
            f"{MAX_LOAD_TRIGGERED_REQUESTS}. Each one takes its own database connection at the same moment as all "
            "the others, which is what exhausted the pool on the dev deployment. Load the new one on demand, or "
            "make the case for raising the budget.",
        )

    def test_the_counter_actually_finds_them(self) -> None:
        """Guards the test itself.

        A regex that silently matched nothing would make the budget above pass
        forever, which is the failure mode a ceiling test is most prone to - it
        looks green either way.
        """
        html = self._rendered()

        self.assertGreater(
            len(_LOAD_TRIGGER.findall(html)),
            5,
            "the load-trigger pattern matched almost nothing, so the budget assertion is not measuring anything. "
            'Has the page stopped using hx-trigger="load", or has the attribute quoting changed?',
        )
