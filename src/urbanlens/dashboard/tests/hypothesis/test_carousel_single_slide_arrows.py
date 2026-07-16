"""Street View / Satellite Imagery carousels must not show prev/next arrows for a single slide.

Rendered directly against the template with a controlled `slides` list rather
than through the real controller endpoints, since those endpoints call out to
several live external imagery APIs (Google, Esri, USGS, Mapillary, ...) to
build that list - the arrow-visibility logic itself only depends on the
slide count, which this isolates cleanly.
"""

from __future__ import annotations

from django.template.loader import render_to_string

from urbanlens.core.tests.testcase import TestCase

_STREET_VIEW_SLIDE = {"source": "google", "date": "2024", "heading": None, "latitude": 41.0, "longitude": -73.0, "img_src": "https://example.com/a.jpg"}
_SATELLITE_SLIDE = {"source": "esri", "date": "2024", "detail": "High-res", "img_src": "https://example.com/a.jpg"}


class StreetViewCarouselArrowTests(TestCase):
    def test_arrows_hidden_with_a_single_slide(self) -> None:
        html = render_to_string("dashboard/pages/location/street_view.html", {"slides": [_STREET_VIEW_SLIDE], "debug_entries": []})
        self.assertNotIn("sv-prev", html)
        self.assertNotIn("sv-next", html)

    def test_arrows_shown_with_multiple_slides(self) -> None:
        html = render_to_string("dashboard/pages/location/street_view.html", {"slides": [_STREET_VIEW_SLIDE, dict(_STREET_VIEW_SLIDE, source="mapillary")], "debug_entries": []})
        self.assertIn("sv-prev", html)
        self.assertIn("sv-next", html)


class SatelliteViewCarouselArrowTests(TestCase):
    def test_arrows_hidden_with_a_single_slide(self) -> None:
        html = render_to_string("dashboard/pages/location/satellite_view.html", {"slides": [_SATELLITE_SLIDE], "debug_entries": []})
        self.assertNotIn("sat-prev", html)
        self.assertNotIn("sat-next", html)

    def test_arrows_shown_with_multiple_slides(self) -> None:
        html = render_to_string("dashboard/pages/location/satellite_view.html", {"slides": [_SATELLITE_SLIDE, dict(_SATELLITE_SLIDE, source="usgs")], "debug_entries": []})
        self.assertIn("sat-prev", html)
        self.assertIn("sat-next", html)
