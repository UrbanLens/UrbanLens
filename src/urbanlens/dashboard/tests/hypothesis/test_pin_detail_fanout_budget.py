"""How many requests the Private Pin page fires the moment it opens.

The integration suite found this page exhausting the database connection pool:
it loads its enrichment panels with `hx-trigger="load"`, so opening it issues
roughly thirty requests at once, each of which is a Django request taking its own
connection (`CONN_MAX_AGE` is 0). Against a Postgres with the default
`max_connections`, a few simultaneous readers is enough to start answering 500s
from whichever panel arrives when the pool is full.

**This test cannot reproduce that**, and it is worth being clear about the
limits rather than implying otherwise. Exhaustion needs concurrency against a
real pool, which does not exist in a suite that issues one request at a time -
see `docs/audits/TEST_COVERAGE_GAPS.md`, where the pool itself is listed as
integration-only. What a unit test *can* hold is the shape of the page.

Two things are held here, and the second is now the load-bearing one.

The **count** is a ratchet: it stops the number of load-triggered elements
growing. It was the only guard while the fix was undecided, and it is no longer
the whole story, because the count is no longer the concurrency - the enrichment
panels queue against shared `hx-sync` lanes now, so twelve of them in one lane
are one request at a time rather than twelve at once.

The **lanes** are the invariant that replaced it. Every enrichment panel on that
page must name a lane; a new one added without `hx-sync` re-opens the problem
while leaving the count assertion green, which is exactly the regression a
ceiling test cannot see. Measured in a browser against the dev stack: the page
went from 58 simultaneous requests to 27, reaching the same settled state.
"""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

#: The most load-triggered HTMX requests the Private Pin page may fire.
#:
#: Was 53 at the time of writing; lowered to **48** when the Visits/Photos/
#: Article/Notes/Edit-History subnav tabs switched from `hx-trigger="load"` to
#: `"revealed"` - those five panels sit inside a client-side-hidden
#: `<section data-tab-panel>` that only the tab switch ever unhides (the same
#: eager-hidden-tab defect independently found and fixed on the Organize page),
#: so `load` was firing them on every page view regardless of which tab was
#: open. This is still a ratchet, not an endorsement: the remaining 48 are
#: already more than the dev deployment's Postgres could serve concurrently;
#: the budget exists to stop it growing while the real fix - loading panels in
#: waves, or behind one request - is decided.
#:
#: Not all 48 fire on every load: some carry a filter
#: (`load[!window.ulSectionCollapsed(...)]`) and stay quiet for a collapsed
#: section, which is why the deployment showed around thirty concurrent requests
#: rather than the static count. The static count is still the right thing to
#: bound, because it is the ceiling a user with everything expanded actually
#: reaches.
MAX_LOAD_TRIGGERED_REQUESTS = 48

#: An element that fetches as soon as the page loads. `load` may carry a filter
#: (`load[!window.ulSectionCollapsed(...)]`) or sit alongside other triggers, so
#: the match is on the word rather than the whole attribute.
_LOAD_TRIGGER = re.compile(r'hx-trigger="[^"]*\bload\b[^"]*"')

#: An element whose job is to enrich the page rather than to render it: an
#: external-data panel, a collapsible section, or one of the media-gallery's
#: per-provider loaders. These are the ones that must queue. The page's own
#: content - the overview, the gallery frame, the boundary - is deliberately
#: not laned, because delaying it delays the page itself.
_ENRICHMENT_MARKERS = ("data-ext-panel-204", "data-collapse-section", "media-provider-loader")

#: A queue lane, as `hx-sync` spells it.
_LANE = re.compile(r'hx-sync="#pin-(?:panel|media)-lane-[^"]*:queue all"')

#: Opening tags, so a marker on one element is not credited to its neighbour.
_OPEN_TAG = re.compile(r"<div [^>]*>", re.DOTALL)


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
        self.assertEqual(response.status_code, 200, "the Private Pin page did not render, so nothing was counted")
        return response.content.decode()

    def test_the_page_does_not_fire_more_requests_on_load_than_its_budget(self) -> None:
        html = self._rendered()
        count = len(_LOAD_TRIGGER.findall(html))

        self.assertLessEqual(
            count,
            MAX_LOAD_TRIGGERED_REQUESTS,
            f"the Private Pin page now fires {count} requests the moment it opens, over its budget of "
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

    def test_every_enrichment_panel_queues_against_a_lane(self) -> None:
        """The invariant the count assertion cannot see.

        A panel added without `hx-sync` fires alongside every other one and puts
        the page back where it was, while the ceiling above stays green because
        the ceiling counts elements rather than simultaneous requests. This is
        what actually holds the concurrency down.

        Matched per opening tag rather than over the whole document, so a lane on
        one panel is never credited to the panel below it.
        """
        html = self._rendered()

        unlaned = [
            tag[:160]
            for tag in _OPEN_TAG.findall(html)
            if any(marker in tag for marker in _ENRICHMENT_MARKERS)
            and _LOAD_TRIGGER.search(tag)
            and not _LANE.search(tag)
        ]

        self.assertEqual(
            unlaned,
            [],
            f"{len(unlaned)} enrichment panel(s) on the Private Pin page fetch on load without queueing against a "
            'lane, so they fire alongside every other panel. Add hx-sync="#pin-panel-lane-N:queue all" (or '
            "#pin-media-lane-N for a gallery provider), picking the lane with the fewest panels already on it. "
            f"First offender: {unlaned[0] if unlaned else ''}",
        )

    def test_the_lane_check_actually_finds_panels(self) -> None:
        """Guards the guard: a marker list that matched nothing would pass forever."""
        html = self._rendered()

        laned = [tag for tag in _OPEN_TAG.findall(html) if _LANE.search(tag)]

        self.assertGreater(
            len(laned),
            10,
            "almost no laned panels were found, so the lane assertion is not measuring anything. Have the "
            "hx-sync attributes been removed, or the lane naming changed?",
        )
