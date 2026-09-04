"""Integration coverage for grandfathered access after a real-estate parcel split.

Two mechanisms, tested separately because they are triggered independently and
must not be confused with each other:

- **Split-family permanence** (:class:`GrandfatheredParcelSplitAccessTests`).
  When a parcel (M) splits into successors (N, O, P -
  ``services.places.splits.process_split``), every profile who held the
  undivided parcel is permanently granted the *whole* family - the split and
  every successor together, regardless of which one their own pin
  re-resolves onto (:meth:`PlaceAccessGrantManager.snapshot_family`). A
  profile who was never a prior holder can still reach the family the
  ordinary way - by independently pinning every current successor at once -
  and the moment that happens they are *also* permanently snapshotted (see
  ``wiki_access._snapshot_earned_split_families``). Confirmed with Jess
  2026-08-31: grandfathering here is truly permanent - once granted, no
  amount of unpinning ever takes it away again. A brand-new profile who
  holds fewer than every current successor gets none of it, and the parent
  wiki must never be discoverable (including as a candidate on the pin
  detail page's linked-wikis list, see ``services.places.ambiguity.
  linked_wiki_locations``) until it's actually earned.

- **Engagement grandfathering** (:class:`WikiEngagementGrandfatheringTests`).
  Independent of any split: a profile who actually viewed a wiki, or shared
  content to it, while they held access keeps that access even after every
  qualifying pin is later moved or deleted
  (:meth:`PlaceAccessGrantManager.record_engagement`, called from
  ``wiki_access.resolve_visible_wiki`` and
  ``wiki_share.WikiShareService.share_from_pin``). A profile who never
  engaged with a wiki loses it the moment their last qualifying pin is gone,
  exactly as before this existed.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant, PlaceKind, PlaceRelation
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.places.ambiguity import linked_wiki_locations
from urbanlens.dashboard.services.places.splits import process_split
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to
from urbanlens.dashboard.services.wiki.wiki_share import WikiShareService

from .test_places_access_predicate import pin_on
from .test_places_campus import make_place, square


def _rect(lng_min: float, lng_max: float, lat_min: float, lat_max: float) -> MultiPolygon:
    """An axis-aligned rectangle, for three side-by-side split successors."""
    ring = ((lng_min, lat_min), (lng_max, lat_min), (lng_max, lat_max), (lng_min, lat_max), (lng_min, lat_min))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class GrandfatheredParcelSplitAccessTests(TestCase):
    """Parcel M splits into successors N, O, P; access to the whole family is earned once and kept forever."""

    def setUp(self) -> None:
        super().setUp()
        self.user_a = baker.make(User).profile
        self.user_b = baker.make(User).profile

        self.parcel = make_place(PlaceKind.PARCEL, square(-74.0, 40.0, 0.03), name="Hudson River State Hospital")
        # Three disjoint vertical strips inside the parcel's outline.
        self.n_geom = _rect(-74.03, -74.01, 39.97, 40.03)
        self.o_geom = _rect(-74.01, -73.99, 39.97, 40.03)
        self.p_geom = _rect(-73.99, -73.97, 39.97, 40.03)

        # User A pins the undivided parcel before any split exists.
        self.pin_a = pin_on(self.user_a, self.parcel, lat=40.0, lng=-74.02)
        self.wiki_m = baker.make(Wiki, location=self.pin_a.location, place=self.parcel)

    def _split(self) -> None:
        """Perform the real split, then resolve N/O/P and give each a wiki."""
        process_split(self.parcel, [self.n_geom, self.o_geom, self.p_geom])
        self.parcel.refresh_from_db()
        self.place_n = self._successor_at(-74.02, 40.0)
        self.place_o = self._successor_at(-74.00, 40.0)
        self.place_p = self._successor_at(-73.98, 40.0)

        loc_n = Location.objects.create(latitude=40.0, longitude=-74.021)
        resolution.attach_location(loc_n, self.place_n)
        loc_o = Location.objects.create(latitude=40.0, longitude=-74.001)
        resolution.attach_location(loc_o, self.place_o)
        loc_p = Location.objects.create(latitude=40.0, longitude=-73.981)
        resolution.attach_location(loc_p, self.place_p)
        self.wiki_n = baker.make(Wiki, location=loc_n, place=self.place_n)
        self.wiki_o = baker.make(Wiki, location=loc_o, place=self.place_o)
        self.wiki_p = baker.make(Wiki, location=loc_p, place=self.place_p)

    def _successor_at(self, lng: float, lat: float) -> Place:
        point = Point(lng, lat, srid=4326)
        for child in self.parcel.children.filter(parent_relation=PlaceRelation.MEMBER_OF):
            if child.geometry is not None and child.geometry.contains(point):
                return child
        raise AssertionError(f"No split successor contains ({lat}, {lng})")

    def _sees(self, profile) -> set[str]:
        """Which of {m, n, o, p} a profile currently sees, for terse assertions."""
        wikis = {"m": self.wiki_m, "n": self.wiki_n, "o": self.wiki_o, "p": self.wiki_p}
        return {key for key, wiki in wikis.items() if location_visible_to(wiki.location, profile)}

    # ── User A: held the undivided parcel before the split ──────────────────

    def test_user_a_gains_the_whole_family_from_a_single_pre_split_pin(self) -> None:
        self.assertTrue(location_visible_to(self.wiki_m.location, self.user_a))  # before the split, only M exists
        self._split()
        self.assertEqual(self._sees(self.user_a), {"m", "n", "o", "p"})

    def test_user_a_keeps_the_whole_family_after_unpinning_everything(self) -> None:
        self._split()
        self.assertEqual(self._sees(self.user_a), {"m", "n", "o", "p"})

        self.pin_a.delete()  # his only pin, now resolved onto N
        self.assertEqual(Pin.objects.filter(profile=self.user_a).count(), 0)
        self.assertEqual(
            self._sees(self.user_a),
            {"m", "n", "o", "p"},
            "grandfathering is permanent - losing every pin must not take it away",
        )

        # Re-pinning a *different* successor changes nothing: he already had it all.
        pin_on(self.user_a, self.place_p, lat=40.0, lng=-73.98)
        self.assertEqual(self._sees(self.user_a), {"m", "n", "o", "p"})

    # ── User B: a new profile, never held the undivided parcel ─────────────

    def test_user_b_must_earn_every_current_successor_before_reaching_the_parent(self) -> None:
        self._split()
        pin_on(self.user_b, self.place_n, lat=40.0, lng=-74.019)
        self.assertEqual(self._sees(self.user_b), {"n"})

        pin_on(self.user_b, self.place_o, lat=40.0, lng=-73.999)
        self.assertEqual(self._sees(self.user_b), {"n", "o"}, "missing P - M must stay unreached")

        pin_on(self.user_b, self.place_p, lat=40.0, lng=-73.979)
        self.assertEqual(
            self._sees(self.user_b),
            {"m", "n", "o", "p"},
            "holding every current successor earns the whole family, M included",
        )

    def test_user_b_cannot_discover_the_parent_wiki_before_earning_it(self) -> None:
        self._split()
        pin_n = pin_on(self.user_b, self.place_n, lat=40.0, lng=-74.019)
        pin_on(self.user_b, self.place_o, lat=40.0, lng=-73.999)  # missing P - M still unearned

        self.assertFalse(location_visible_to(self.wiki_m.location, self.user_b))
        locations = linked_wiki_locations(pin_n, self.user_b)
        self.assertNotIn(self.wiki_m.location, locations)

    def test_user_b_keeps_the_family_once_earned_even_after_unpinning_everything(self) -> None:
        self._split()
        pin_n = pin_on(self.user_b, self.place_n, lat=40.0, lng=-74.019)
        pin_o = pin_on(self.user_b, self.place_o, lat=40.0, lng=-73.999)
        pin_p = pin_on(self.user_b, self.place_p, lat=40.0, lng=-73.979)
        self.assertEqual(self._sees(self.user_b), {"m", "n", "o", "p"})

        pin_o.delete()
        self.assertEqual(
            self._sees(self.user_b), {"m", "n", "o", "p"}, "earned once, permanent - one missing pin changes nothing"
        )

        pin_p.delete()
        self.assertEqual(self._sees(self.user_b), {"m", "n", "o", "p"})

        pin_n.delete()
        self.assertEqual(Pin.objects.filter(profile=self.user_b).count(), 0)
        self.assertEqual(
            self._sees(self.user_b), {"m", "n", "o", "p"}, "zero pins left anywhere in the family - still permanent"
        )

        # A distinct coordinate: pin_n.delete() removed the Pin, not the
        # underlying (latitude, longitude)-unique Location row.
        pin_on(self.user_b, self.place_n, lat=40.0, lng=-74.0191)
        self.assertEqual(self._sees(self.user_b), {"m", "n", "o", "p"})

    def test_the_grants_are_actually_recorded_with_the_split_reason(self) -> None:
        """The mechanism, not just its effect: real permanent rows, not a cache."""
        self._split()
        pin_on(self.user_b, self.place_n, lat=40.0, lng=-74.019)
        pin_on(self.user_b, self.place_o, lat=40.0, lng=-73.999)
        pin_on(self.user_b, self.place_p, lat=40.0, lng=-73.979)
        location_visible_to(self.wiki_m.location, self.user_b)  # triggers the snapshot

        granted_places = set(
            PlaceAccessGrant.objects.filter(profile=self.user_b, reason=GrantReason.GRANDFATHERED_SPLIT).values_list(
                "place_id", flat=True
            )
        )
        self.assertEqual(granted_places, {self.parcel.pk, self.place_n.pk, self.place_o.pk, self.place_p.pk})

        # User A's split-time snapshot covers the identical set.
        granted_a = set(
            PlaceAccessGrant.objects.filter(profile=self.user_a, reason=GrantReason.GRANDFATHERED_SPLIT).values_list(
                "place_id", flat=True
            )
        )
        self.assertEqual(granted_a, {self.parcel.pk, self.place_n.pk, self.place_o.pk, self.place_p.pk})

    def test_an_unrelated_profile_reaches_nothing(self) -> None:
        """Sanity check: none of this leaks to a profile who never touched the family."""
        self._split()
        stranger = baker.make(User).profile
        self.assertEqual(self._sees(stranger), set())


class WikiEngagementGrandfatheringTests(TestCase):
    """An ordinary (never-split) parcel: viewing or sharing while access is held
    grandfathers a profile in permanently; never doing either does not."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make(User).profile
        self.sharer = baker.make(User).profile
        self.silent = baker.make(User).profile

        self.place_x = make_place(PlaceKind.PARCEL, square(-73.0, 41.0, 0.002))
        self.place_y = make_place(PlaceKind.PARCEL, square(-72.0, 41.0, 0.002))
        self.place_z = make_place(PlaceKind.PARCEL, square(-71.0, 41.0, 0.002))

        self.pin_x = pin_on(self.viewer, self.place_x, lat=41.0, lng=-73.0)
        self.wiki_x = baker.make(Wiki, location=self.pin_x.location, place=self.place_x)

        self.pin_y = pin_on(self.sharer, self.place_y, lat=41.0, lng=-72.0)
        self.wiki_y = baker.make(Wiki, location=self.pin_y.location, place=self.place_y)

        self.pin_z = pin_on(self.silent, self.place_z, lat=41.0, lng=-71.0)
        self.wiki_z = baker.make(Wiki, location=self.pin_z.location, place=self.place_z)

    def test_viewing_the_wiki_grandfathers_the_viewer_after_the_pin_is_gone(self) -> None:
        self.assertTrue(location_visible_to(self.wiki_x.location, self.viewer))

        self.client.force_login(self.viewer.user)
        response = self.client.get(reverse("location.wiki", args=[self.wiki_x.location.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PlaceAccessGrant.objects.filter(
                profile=self.viewer, place=self.place_x, reason=GrantReason.GRANDFATHERED_ENGAGEMENT
            ).exists()
        )

        self.pin_x.delete()
        self.assertTrue(
            location_visible_to(self.wiki_x.location, self.viewer),
            "viewed it while access was held - must survive losing the pin",
        )

    def test_sharing_content_grandfathers_the_sharer_after_the_pin_is_gone(self) -> None:
        self.assertTrue(location_visible_to(self.wiki_y.location, self.sharer))
        self.pin_y.danger = 4
        self.pin_y.save(update_fields=["danger"])

        wiki, shared = WikiShareService().share_from_pin(self.pin_y, include_fields={"danger"})
        self.assertTrue(shared)
        self.assertTrue(
            PlaceAccessGrant.objects.filter(
                profile=self.sharer, place=self.place_y, reason=GrantReason.GRANDFATHERED_ENGAGEMENT
            ).exists()
        )

        self.pin_y.delete()
        self.assertTrue(
            location_visible_to(self.wiki_y.location, self.sharer),
            "shared content while access was held - must survive losing the pin",
        )

    def test_never_viewing_or_sharing_loses_access_once_the_last_pin_is_gone(self) -> None:
        self.assertTrue(location_visible_to(self.wiki_z.location, self.silent))
        self.assertFalse(PlaceAccessGrant.objects.filter(profile=self.silent, place=self.place_z).exists())

        self.pin_z.delete()
        self.assertFalse(
            location_visible_to(self.wiki_z.location, self.silent),
            "never viewed or shared - no pin left, no grandfathering",
        )

    def test_opening_the_share_dialog_without_contributing_anything_does_not_grandfather(self) -> None:
        """Gated on `shared`, not merely reaching the method - see wiki_share.py."""
        wiki, shared = WikiShareService().share_from_pin(
            self.pin_y, include_fields=set(), alias_ids=set(), image_ids=set()
        )
        self.assertFalse(shared)
        self.assertFalse(
            PlaceAccessGrant.objects.filter(
                profile=self.sharer, place=self.place_y, reason=GrantReason.GRANDFATHERED_ENGAGEMENT
            ).exists()
        )

    def test_a_stranger_viewing_a_page_they_cannot_see_grants_nothing(self) -> None:
        """resolve_visible_wiki 404s before the engagement hook is ever reached."""
        stranger = baker.make(User).profile
        self.client.force_login(stranger.user)
        response = self.client.get(reverse("location.wiki", args=[self.wiki_x.location.slug]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PlaceAccessGrant.objects.filter(profile=stranger, place=self.place_x).exists())
