"""The external API's interactive documentation runs under the site's Content-Security-Policy (P312).

The page loaded Swagger UI from ``cdn.jsdelivr.net/npm/swagger-ui-dist@latest`` and started it with an inline script.
``script-src`` admits neither, so the browser blocked both and the page rendered an empty body.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.test import Client
from django.urls import reverse

from urbanlens.core.tests.testcase import SimpleTestCase


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_scripts = 0
        self.stylesheets: list[str] = []
        self._in_script_without_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script":
            self.scripts.append(attributes)
            self._in_script_without_src = not attributes.get("src")
        elif tag == "link" and attributes.get("rel") == "stylesheet" and attributes.get("href"):
            self.stylesheets.append(str(attributes["href"]))

    def handle_data(self, data: str) -> None:
        if self._in_script_without_src and data.strip():
            self.inline_scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script_without_src = False


def _policy(response) -> dict[str, list[str]]:
    header = response.headers.get("Content-Security-Policy") or response.headers["Content-Security-Policy-Report-Only"]
    directives: dict[str, list[str]] = {}
    for directive in header.split(";"):
        name, *sources = directive.split()
        directives[name] = sources
    return directives


def _admits(sources: list[str], url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc:
        return "'self'" in sources
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return any(source in {origin, f"{parsed.scheme}://{parsed.hostname}"} for source in sources)


class TheDocsPageRunsUnderItsPolicyTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:docs")
        self.response = Client().get(self.url)
        self.tags = _Tags()
        self.tags.feed(self.response.content.decode())

    def test_the_page_is_served(self) -> None:
        self.assertEqual(self.response.status_code, 200)
        self.assertTrue(self.tags.scripts, "the page loads no script, so there is nothing to check")

    def test_every_script_it_loads_is_admitted(self) -> None:
        sources = _policy(self.response)["script-src"]
        for script in self.tags.scripts:
            with self.subTest(script=script):
                src = script.get("src")
                self.assertTrue(src, "an inline script, which script-src refuses")
                self.assertTrue(_admits(sources, urljoin(self.url, str(src))), f"{src} is not admitted by script-src")
        self.assertEqual(self.tags.inline_scripts, 0)

    def test_every_stylesheet_it_loads_is_admitted(self) -> None:
        sources = _policy(self.response)["style-src"]
        for href in self.tags.stylesheets:
            with self.subTest(href=href):
                self.assertTrue(_admits(sources, urljoin(self.url, href)), f"{href} is not admitted by style-src")

    def test_the_script_that_starts_it_is_served(self) -> None:
        """The boot script, moved out of the page, answers as JavaScript rather than as the page again."""
        own = [
            str(script["src"])
            for script in self.tags.scripts
            if script.get("src") and not urlparse(str(script["src"])).netloc
        ]
        boot = [src for src in own if not src.startswith(settings.STATIC_URL)]
        self.assertEqual(len(boot), 1, own)

        response = Client().get(boot[0])

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["Content-Type"])
        self.assertIn("SwaggerUIBundle", response.content.decode())
