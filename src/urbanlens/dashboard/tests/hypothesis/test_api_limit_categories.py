"""Every REData service key reaches a tab on the API limits page.

``_API_LIMIT_CATEGORIES`` is a hand-curated map, and its own comment records
that anything absent falls into "Other" so a new service is never hidden. That
fallback is deliberate and worth keeping - but it stops being graceful once a
family outgrows it. As REData added a domain per pin-detail panel, 18 of its 32
service keys ended up in the catch-all tab at once, which is not a tab so much
as a second, unsorted list.

Scoped to ``redata_*`` on purpose. The rest of the map covers vendors added one
at a time, where "Other" really is a reasonable landing spot until someone
looks; REData grows in batches and is the family that actually drifted.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.controllers.site_admin import _API_LIMIT_CATEGORIES
from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults


class RedataServiceCategoryCoverageTests(SimpleTestCase):
    def test_every_redata_service_key_has_a_tab(self) -> None:
        keys = {key for key in all_service_defaults() if key.startswith("redata_")}
        self.assertTrue(keys, "no redata service keys were discovered - the registry lookup has moved")

        uncategorized = sorted(keys - set(_API_LIMIT_CATEGORIES))

        self.assertEqual(
            uncategorized,
            [],
            "these REData services would render under 'Other' on /site-admin/api-limits/. "
            "Add each to _API_LIMIT_CATEGORIES in controllers/site_admin.py.",
        )
