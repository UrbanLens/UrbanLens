"""An unrouted URL answers 404, including under `/dashboard/`.

`dashboard/urls.py` ended with its own catch-all::

    re_path(".*", TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name="404")

`TemplateView` has no `status`, so it renders the 404 *page* with a **200**
status. Because that pattern lives inside the `dashboard/` include, it matched
first and shadowed the root URLconf's catch-all - which calls
`_render_404_page` and does set `status=404`, and which every other prefix on
the site reaches correctly. So the one prefix that holds essentially the whole
application was the one answering 200 to a URL that does not exist.

Three things that costs, none of them visible from the page:

* Any `fetch()` that branches on `response.ok` treats a removed or renamed
  endpoint as success, and then parses an HTML error page as JSON. There are
  ~40 raw `fetch()` call sites (P11); this turns a clean failure into a
  confusing one for all of them.
* Alerting on 4xx rates cannot see a broken internal link at all.
* A crawler indexes every mistyped path as a real page (a soft 404).

The fix is deleting that line: `handler404` and the root catch-all already
render the same template, with the right status. The test asserts the status
rather than the markup, because rendering the styled page was never the part
that was wrong.
"""

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
