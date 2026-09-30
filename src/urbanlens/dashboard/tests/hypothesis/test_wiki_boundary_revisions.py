"""A wiki boundary edit references immutable outline snapshots rather than carrying WKT (N29 G4-29).

Each edit stored the whole outline twice as WKT text inside ``WikiEdit.changes`` - up to 50,000 vertices each - and
the web, API and revert paths each wrote it their own way. Outlines now live once each in ``BoundaryRevision``, and
the edit names them by id.
"""

from __future__ import annotations

import importlib
import json
from unittest import mock

from django.apps import apps
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import BoundaryRevision, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

_SCHEDULE = "urbanlens.dashboard.controllers.boundary.schedule_location_boundary_generation"


def _square(delta: float) -> MultiPolygon:
    lng, lat = -74.0, 40.0
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _WikiCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def _draw(self, polygon: MultiPolygon | None) -> WikiEdit:
        body = {"boundary_type": "property", "polygon": json.loads(polygon.geojson) if polygon else None}
        with mock.patch(_SCHEDULE, return_value=False):
            response = self.client.post(
                reverse("location.wiki.boundary", args=[self.location.slug]),
                data=json.dumps(body),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        return WikiEdit.objects.filter(wiki=self.wiki).latest("pk")


class BoundaryEditShapeTests(_WikiCase):
    def test_an_edit_names_revisions_and_carries_no_geometry(self) -> None:
        edit = self._draw(_square(0.001))

        change = edit.changes["boundary_property"]
        self.assertIsNone(change["from"])
        self.assertIsInstance(change["to"], int)
        self.assertTrue(BoundaryRevision.objects.get(pk=change["to"], wiki=self.wiki).polygon.equals(_square(0.001)))
        self.assertNotIn("POLYGON", json.dumps(edit.changes))

    def test_consecutive_edits_share_the_outline_between_them(self) -> None:
        first = self._draw(_square(0.001))
        second = self._draw(_square(0.002))
        cleared = self._draw(None)

        self.assertEqual(second.changes["boundary_property"]["from"], first.changes["boundary_property"]["to"])
        self.assertEqual(cleared.changes["boundary_property"]["from"], second.changes["boundary_property"]["to"])
        self.assertIsNone(cleared.changes["boundary_property"]["to"])
        self.assertEqual(BoundaryRevision.objects.filter(wiki=self.wiki).count(), 2)

    def test_the_external_api_writes_the_same_shape(self) -> None:
        from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        _key, raw_key = generate_api_key(self.user, "t")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])
        with mock.patch(
            "urbanlens.dashboard.external_api.views_wiki.schedule_location_boundary_generation", return_value=False
        ):
            response = self.client.post(
                f"/dashboard/api/external/v1/wikis/{self.location.ensure_slug()}/boundary/",
                {"boundary_type": "property", "polygon": json.loads(_square(0.001).geojson)},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {raw_key}",
            )
        self.assertEqual(response.status_code, 200, response.content)

        change = WikiEdit.objects.filter(wiki=self.wiki).latest("pk").changes["boundary_property"]
        self.assertIsInstance(change["to"], int)
        self.assertTrue(BoundaryRevision.objects.get(pk=change["to"]).polygon.equals(_square(0.001)))


class BoundaryHistoryTests(_WikiCase):
    def test_reverting_through_the_view_restores_the_prior_outline(self) -> None:
        from urbanlens.dashboard.models.boundary.model import Boundary

        self._draw(_square(0.001))
        second = self._draw(_square(0.002))

        with mock.patch(_SCHEDULE, return_value=False):
            self.client.post(reverse("location.wiki.revert", args=[self.location.slug, second.pk]))

        row = Boundary.objects.row_for_wiki(self.wiki, BoundaryType.PROPERTY)
        self.assertTrue(row.polygon.equals(_square(0.001)))
        revert = WikiEdit.objects.filter(wiki=self.wiki, is_revert=True).get()
        self.assertEqual(revert.changes["boundary_property"]["to"], second.changes["boundary_property"]["from"])

    def test_expunging_an_edit_erases_the_outline_it_introduced(self) -> None:
        first = self._draw(_square(0.001))
        introduced = first.changes["boundary_property"]["to"]

        self.client.post(reverse("location.wiki.history.delete", args=[self.location.slug, first.pk]))

        self.assertFalse(WikiEdit.objects.filter(pk=first.pk).exists())
        self.assertFalse(BoundaryRevision.objects.filter(pk=introduced).exists())

    def test_the_history_page_describes_an_outline_rather_than_printing_an_id(self) -> None:
        self._draw(_square(0.001))

        response = self.client.get(reverse("location.wiki.history", args=[self.location.slug]))

        self.assertContains(response, "Drawn outline")


class LegacyEditConversionTests(TestCase):
    def test_the_migration_turns_inline_wkt_into_revisions(self) -> None:
        convert = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")._0101_convert_inline_boundaries
        wiki = baker.make("dashboard.Wiki", location=baker.make(Location, latitude="40.0", longitude="-74.0"))
        first = baker.make(
            WikiEdit,
            wiki=wiki,
            changes={"bounding_box": {"from": None, "to": _square(0.001).wkt}, "name": {"from": "a", "to": "b"}},
        )
        second = baker.make(
            WikiEdit, wiki=wiki, changes={"boundary_property": {"from": _square(0.001).wkt, "to": "not wkt"}}
        )

        convert(apps, None)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.changes["name"], {"from": "a", "to": "b"})
        self.assertNotIn("bounding_box", first.changes)
        introduced = first.changes["boundary_property"]["to"]
        self.assertTrue(BoundaryRevision.objects.get(pk=introduced).polygon.equals(_square(0.001)))
        self.assertEqual(second.changes["boundary_property"], {"from": introduced, "to": None})
        self.assertEqual(BoundaryRevision.objects.filter(wiki=wiki).count(), 1)
