"""Every REData service key reaches a tab on the API limits page."""

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
