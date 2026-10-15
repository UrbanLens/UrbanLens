"""The default CID resolver chain: Google is the core fallback, and a plugin's resolver answers ahead of it."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import CID_RESOLVERS
from urbanlens.dashboard.services.apis.locations.cid_resolution import GoogleCidResolver


class GoogleCidFallbackTests(SimpleTestCase):
    def test_the_default_chain_contains_google_from_the_google_places_builtin(self) -> None:
        google = [
            contribution
            for contribution in CID_RESOLVERS.contributions()
            if isinstance(contribution.value, GoogleCidResolver)
        ]

        self.assertEqual(len(google), 1)
        self.assertEqual(google[0].owner, "google_places")
        self.assertEqual(google[0].priority, 100)
