"""Tests for the "this move ends your wiki access" confirmation.

Wiki visibility is derived, not stored: a profile sees a place's community wiki
because one of their pins sits at (or inside the official boundary of) that
place. Dragging such a pin away therefore revokes their own access silently -
the wiki page simply starts 404ing, indistinguishable from one that never
existed. Rather than let that happen invisibly, a move that would cost the
owner access is refused once with 409 and a list of what's at stake, and goes
through when re-sent with ``confirm_wiki_loss``.

The check is advisory: it previews ``location_visible_to`` rather than gating
on it, so a wrong answer can only mis-warn, never grant access.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, wikis_hidden_by_pin_move

from .place_helpers import official_geometry


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class PinMoveWikiLossTests(TestCase):
    """PATCH /dashboard/rest/pins/<uuid>/ guards moves that cost wiki access."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        official_geometry(self.wiki_location, _square(-74.0, 40.0, 0.003))
        self.wiki = baker.make(Wiki, location=self.wiki_location, name="Old Asylum")

        # The owner's pin sits inside the wiki's boundary, on its own Location.
        self.pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.0005, longitude=-74.0005))

    def _patch(self, **body):
        return self.client.patch(
            reverse("pins-detail", kwargs={"uuid": self.pin.uuid}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_move_out_of_the_boundary_is_refused_with_409(self) -> None:
        location_before = self.pin.location_id

        response = self._patch(latitude=41.0, longitude=-73.0)

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertTrue(payload["requires_wiki_loss_confirmation"])
        self.assertEqual([w["name"] for w in payload["wikis"]], ["Old Asylum"])
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, location_before)

    def test_confirmed_move_goes_through_and_access_is_actually_lost(self) -> None:
        response = self._patch(latitude=41.0, longitude=-73.0, confirm_wiki_loss=True)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertAlmostEqual(float(self.pin.location.latitude), 41.0, places=6)
        self.assertFalse(location_visible_to(self.wiki_location, self.profile))

    def test_move_within_the_boundary_needs_no_confirmation(self) -> None:
        response = self._patch(latitude=40.001, longitude=-74.001)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_no_warning_when_another_pin_keeps_the_access(self) -> None:
        """Only pins actually holding the access open are worth warning about."""
        baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.001, longitude=-74.001))

        response = self._patch(latitude=41.0, longitude=-73.0)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(location_visible_to(self.wiki_location, self.profile))

    def test_no_warning_when_the_place_has_no_wiki(self) -> None:
        Wiki.objects.filter(pk=self.wiki.pk).delete()

        response = self._patch(latitude=41.0, longitude=-73.0)

        self.assertEqual(response.status_code, 200)

    def test_invalid_coordinates_are_still_rejected_as_400(self) -> None:
        response = self._patch(latitude="not-a-number", longitude=-73.0)
        self.assertEqual(response.status_code, 400)

    def test_a_non_move_update_is_unaffected(self) -> None:
        response = self._patch(name="Renamed")
        self.assertEqual(response.status_code, 200)

    def test_invalid_coordinates_beat_the_confirmation_prompt(self) -> None:
        """Input errors are reported before the user is asked to confirm - being
        prompted and then handed a 400 would be a pointless round trip."""
        response = self._patch(latitude=999.0, longitude=-73.0)

        self.assertEqual(response.status_code, 400)

    def test_a_confirmed_move_still_applies_other_fields(self) -> None:
        response = self._patch(latitude=41.0, longitude=-73.0, name="Renamed", confirm_wiki_loss=True)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Renamed")
        self.assertAlmostEqual(float(self.pin.location.latitude), 41.0, places=6)


class WikisHiddenByPinMoveServiceTests(TestCase):
    """The service behind the warning, exercised directly."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        official_geometry(self.wiki_location, _square(-74.0, 40.0, 0.003))
        self.wiki = baker.make(Wiki, location=self.wiki_location, name="Old Asylum")

    def test_lists_the_wiki_a_move_would_hide(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.0005, longitude=-74.0005))

        self.assertEqual([w.pk for w in wikis_hidden_by_pin_move(pin, 41.0, -73.0)], [self.wiki.pk])

    def test_empty_when_the_pin_stays_inside_the_boundary(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.0005, longitude=-74.0005))

        self.assertEqual(wikis_hidden_by_pin_move(pin, 40.001, -74.001), [])

    def test_empty_for_a_pin_at_a_place_with_no_wiki(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=10.0, longitude=20.0))

        self.assertEqual(wikis_hidden_by_pin_move(pin, 11.0, 21.0), [])

    def test_moving_the_exact_location_pin_away_is_reported(self) -> None:
        """A pin on the wiki's own Location grants access by exact match."""
        pin = baker.make(Pin, profile=self.profile, location=self.wiki_location)

        self.assertEqual([w.pk for w in wikis_hidden_by_pin_move(pin, 41.0, -73.0)], [self.wiki.pk])

    def test_another_profiles_pin_does_not_suppress_the_warning(self) -> None:
        """Someone else's pin at the place says nothing about this owner's access."""
        other = baker.make(User).profile
        baker.make(Pin, profile=other, location=Location.objects.create(latitude=40.001, longitude=-74.001))
        pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.0005, longitude=-74.0005))

        self.assertEqual([w.pk for w in wikis_hidden_by_pin_move(pin, 41.0, -73.0)], [self.wiki.pk])

    def test_a_stay_put_move_is_not_reported_as_a_loss(self) -> None:
        """Dropping the marker where it already was must not warn - including at
        a place with no boundary data, where exact match is the only grant and
        the moved pin is excluded from the "other pins" set."""
        bare_location = Location.objects.create(latitude=12.0, longitude=34.0)
        baker.make(Wiki, location=bare_location, name="No Boundary Yet")
        pin = baker.make(Pin, profile=self.profile, location=bare_location)

        self.assertEqual(wikis_hidden_by_pin_move(pin, 12.0, 34.0), [])

    def test_moving_away_from_a_boundaryless_place_is_reported(self) -> None:
        """The counterpart: with no boundary, any real move does lose access."""
        bare_location = Location.objects.create(latitude=12.0, longitude=34.0)
        wiki = baker.make(Wiki, location=bare_location, name="No Boundary Yet")
        pin = baker.make(Pin, profile=self.profile, location=bare_location)

        self.assertEqual([w.pk for w in wikis_hidden_by_pin_move(pin, 12.5, 34.5)], [wiki.pk])

    def test_a_wiki_the_owner_cannot_see_is_never_listed(self) -> None:
        """Nothing to lose if they never had access - and listing it would leak
        that the wiki exists at all."""
        far_wiki_location = Location.objects.create(latitude=10.0, longitude=20.0)
        official_geometry(far_wiki_location, _square(20.0, 10.0, 0.003))
        baker.make(Wiki, location=far_wiki_location, name="Somewhere Else")
        pin = baker.make(Pin, profile=self.profile, location=Location.objects.create(latitude=40.0005, longitude=-74.0005))

        names = [w.name for w in wikis_hidden_by_pin_move(pin, 41.0, -73.0)]

        self.assertNotIn("Somewhere Else", names)
