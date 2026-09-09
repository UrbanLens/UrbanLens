"""`WikiBoundaryView` - the community boundary editor - had no test anywhere.

Only its pin-scoped sibling `BoundaryController` was covered (`test_boundary.py`),
so three behaviours specific to the community half went unexercised: the area
limit against `SiteSettings.max_bbox_area_km2`, the `WikiEdit` audit-trail write,
and the `just_drawn` bypass that stops concealment hiding a writer's own save
from that write's own response.

Community drawings are keyed by Wiki, and the shared location-default rows only
ever hold API-generated geometry, so nothing here can influence point→location
matching. That separation is what the "a community edit lands on a wiki-keyed
row" assertions below pin down.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

#: The generation scheduler reaches a Celery task and an external boundary API.
_SCHEDULE = "urbanlens.dashboard.controllers.boundary.schedule_location_boundary_generation"


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class WikiBoundaryViewTests(TestCase):
    """POST /location/<slug>/wiki/boundary/ saves, clears, validates and audits."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Community Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)
        self.url = reverse("location.wiki.boundary", args=[self.location.slug])

    def _post(self, body: object) -> object:
        with mock.patch(_SCHEDULE, return_value=False):
            return self.client.post(self.url, data=json.dumps(body), content_type="application/json")

    # --- saving -----------------------------------------------------------

    def test_a_drawn_polygon_lands_on_a_wiki_keyed_row(self) -> None:
        polygon = _square(-74.0, 40.0, 0.001)
        response = self._post({"boundary_type": "property", "polygon": json.loads(polygon.geojson)})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["ok"])
        row = Boundary.objects.get(wiki=self.wiki, boundary_type=BoundaryType.PROPERTY)
        self.assertIsNotNone(row.polygon)
        self.assertEqual(row.location_id, self.location.pk, "the row carries the wiki's location")

    def test_the_writer_sees_their_own_drawing_in_the_saving_response(self) -> None:
        """`just_drawn`: concealment must not hide this request's own write from it."""
        polygon = _square(-74.0, 40.0, 0.001)
        response = self._post({"boundary_type": "building", "polygon": json.loads(polygon.geojson)})

        payload = json.loads(response.content)
        self.assertIsNotNone(
            payload["boundaries"]["building"]["polygon"], "the save must be visible in its own response"
        )

    def test_a_second_save_replaces_rather_than_accumulates(self) -> None:
        first, second = _square(-74.0, 40.0, 0.001), _square(-74.0, 40.0, 0.002)
        self._post({"boundary_type": "property", "polygon": json.loads(first.geojson)})
        self._post({"boundary_type": "property", "polygon": json.loads(second.geojson)})

        self.assertEqual(Boundary.objects.filter(wiki=self.wiki, boundary_type=BoundaryType.PROPERTY).count(), 1)

    # --- clearing ---------------------------------------------------------

    def test_a_null_polygon_clears_the_row(self) -> None:
        polygon = _square(-74.0, 40.0, 0.001)
        self._post({"boundary_type": "property", "polygon": json.loads(polygon.geojson)})
        self.assertTrue(Boundary.objects.filter(wiki=self.wiki, boundary_type=BoundaryType.PROPERTY).exists())

        response = self._post({"boundary_type": "property", "polygon": None})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Boundary.objects.filter(wiki=self.wiki, boundary_type=BoundaryType.PROPERTY).exists())

    # --- the area limit ---------------------------------------------------

    def test_a_polygon_over_the_site_area_limit_is_rejected(self) -> None:
        settings_row = SiteSettings.get_current()
        settings_row.max_bbox_area_km2 = 1
        settings_row.save(update_fields=["max_bbox_area_km2"])

        # ~1 degree square: several thousand km² at this latitude, far over 1.
        response = self._post({"boundary_type": "property", "polygon": json.loads(_square(-74.0, 40.0, 0.5).geojson)})

        self.assertEqual(response.status_code, 400)
        self.assertIn("too large", json.loads(response.content)["error"])
        self.assertFalse(Boundary.objects.filter(wiki=self.wiki).exists(), "a rejected draw must store nothing")

    def test_a_polygon_under_the_limit_is_accepted(self) -> None:
        """Anti-vacuity: the limit check must not reject everything."""
        settings_row = SiteSettings.get_current()
        settings_row.max_bbox_area_km2 = 1000
        settings_row.save(update_fields=["max_bbox_area_km2"])

        response = self._post({"boundary_type": "property", "polygon": json.loads(_square(-74.0, 40.0, 0.001).geojson)})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Boundary.objects.filter(wiki=self.wiki).exists())

    # --- the audit trail --------------------------------------------------

    def test_a_save_records_a_wiki_edit_naming_both_sides(self) -> None:
        polygon = _square(-74.0, 40.0, 0.001)
        self._post({"boundary_type": "property", "polygon": json.loads(polygon.geojson)})

        edit = WikiEdit.objects.filter(wiki=self.wiki).latest("created")
        self.assertEqual(edit.editor, self.profile)
        change = edit.changes["boundary_property"]
        self.assertIsNone(change["from"], "the first draw has no previous geometry")
        self.assertIsNotNone(change["to"])

    def test_a_clear_records_the_geometry_it_removed(self) -> None:
        polygon = _square(-74.0, 40.0, 0.001)
        self._post({"boundary_type": "property", "polygon": json.loads(polygon.geojson)})
        self._post({"boundary_type": "property", "polygon": None})

        change = WikiEdit.objects.filter(wiki=self.wiki).latest("created").changes["boundary_property"]
        self.assertIsNotNone(change["from"], "a clear must record what was there")
        self.assertIsNone(change["to"])

    # --- request validation -----------------------------------------------

    def test_a_malformed_body_is_a_400(self) -> None:
        with mock.patch(_SCHEDULE, return_value=False):
            response = self.client.post(self.url, data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_a_non_object_body_is_a_400(self) -> None:
        response = self._post(["property"])
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_boundary_type_is_a_400(self) -> None:
        for value in ("parcel", "", None, 7):
            with self.subTest(boundary_type=value):
                response = self._post({"boundary_type": value, "polygon": None})
                self.assertEqual(response.status_code, 400)

    def test_a_malformed_polygon_is_a_400(self) -> None:
        response = self._post({"boundary_type": "property", "polygon": {"type": "Nonsense"}})
        self.assertEqual(response.status_code, 400)

    # --- reading ----------------------------------------------------------

    def test_get_returns_a_typed_payload(self) -> None:
        with mock.patch(_SCHEDULE, return_value=False):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIn("property", payload["boundaries"])
        self.assertIn("building", payload["boundaries"])

    def test_an_anonymous_request_is_redirected_to_login(self) -> None:
        self.client.logout()
        with mock.patch(_SCHEDULE, return_value=False):
            response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 403))
