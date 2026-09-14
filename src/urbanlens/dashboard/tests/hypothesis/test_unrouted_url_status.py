"""An unrouted URL answers 404, including under `/dashboard/`."""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase


class UnroutedUrlStatusTests(TestCase):
    """Every prefix answers 404 for a path nothing routes."""

    #: Prefixes worth naming individually: `/dashboard/` is where the shadowing
    #: catch-all lived, and the others are the neighbours that were already
    #: correct and must stay that way.
    UNROUTED = (
        "/dashboard/this-route-does-not-exist/",
        "/dashboard/map/pin/",
        "/dashboard/rest/no-such-viewset/",
        "/this-route-does-not-exist/",
        "/rest/no-such-viewset/",
    )

    def test_unrouted_urls_answer_404(self) -> None:
        for path in self.UNROUTED:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_404_page_is_still_the_styled_one(self) -> None:
        """Deleting the catch-all must not fall back to Django's plain text."""
        response = self.client.get("/dashboard/this-route-does-not-exist/")

        self.assertEqual(response.status_code, 404)
        self.assertIn("dashboard/pages/errors/404.html", [t.name for t in response.templates])

    def test_a_routed_url_is_unaffected(self) -> None:
        """Guard against 'fixing' this by making the whole prefix 404."""
        self.assertEqual(self.client.get("/dashboard/map/").status_code, 302)
