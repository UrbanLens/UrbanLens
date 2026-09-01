"""A parcel outline a provider offered must beat one we invented.

Reported from the e2e deployment, on the HRSH pin: the boundary drawn on the
Private Pin page is not REData's parcel at all. REData offers six scored
candidates for that parcel and flags one of them ``is_suggested``; the app drew
a convex hull fitted around the pin and its three child pins instead.

Three separate defects compound to produce that, and each is pinned down below
because fixing any one alone still leaves the wrong shape on the map.

**The chain is never asked.** ``resolve_location_place`` answers "what is this
coordinate standing on?" from places already on record - its own docstring says
it "never calls a provider" - but it stamped ``Location.place_resolved_at``
anyway, including on virgin ground where it resolved nothing. That field is
what ``generation_status`` reads as "the provider chain ran", so a pin dropped
somewhere brand new was marked as already-enriched the instant it was created:
``schedule_location_boundary_generation`` returned False, the boundary panel
reported itself ready, and REData was never called. Not slowly, not once -
never, until ``boundary_cache_days`` (60) elapsed.

**A hull we fitted outranks a parcel we were given.** ``resolve_for_pin``
checked the pin's own ``generated_polygon`` second, ahead of the place. A
child-fitted hull is a stand-in for an outline we did not have, so it has to
yield the moment a real one exists - otherwise arriving geometry is invisible
on the very page that asked for it.

**Nothing supersedes the stand-in.** Once real geometry lands, the hull row is
inert but still stored, and ``refit_child_pin_boundary`` kept updating it.

The distinction that matters throughout: a *provider's* outline is evidence
about the world, and a hull around the markers we happen to know about is a
drawing of our own ignorance. The second is a legitimate fallback and a
terrible answer to prefer.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries, boundary_generation_ran, generate_location_boundaries
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.tests.hypothesis.place_helpers import official_geometry

# The real coordinates from the report, so the geometry below is the shape the
# defect was actually seen with rather than an abstract square.
CAMPUS_LAT, CAMPUS_LON = 41.733181, -73.928493


def _square(lon: float, lat: float, size: float) -> MultiPolygon:
    """A square MultiPolygon of *size* degrees centred on (lon, lat)."""
    half = size / 2
    ring = (
        (lon - half, lat - half),
        (lon + half, lat - half),
        (lon + half, lat + half),
        (lon - half, lat + half),
        (lon - half, lat - half),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class CheapResolutionMustNotClaimTheChainRanTests(TestCase):
    """``resolve_location_place`` consults the record; it must not speak for the providers."""

    def _location(self) -> Location:
        return baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LON)

    def test_finding_no_known_place_leaves_the_coordinate_unasked(self) -> None:
        """The reported case: a pin on ground no place covers yet.

        Stamping here is what told the rest of the system the chain had
        already run, so REData was never called for this coordinate at all.
        """
        location = self._location()

        self.assertIsNone(resolution.resolve_location_place(location))

        location.refresh_from_db()
        self.assertIsNone(
            location.place_resolved_at,
            "a lookup that called no provider recorded that the providers had been asked",
        )
        self.assertFalse(
            boundary_generation_ran(location),
            "the coordinate is marked as already enriched, so the provider chain will never be scheduled for it",
        )

    def test_resolving_onto_a_known_place_does_record_the_answer(self) -> None:
        """The cache still has to work: an answer found is an answer stored."""
        location = self._location()
        place = official_geometry(location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01))

        location.refresh_from_db()
        self.assertEqual(location.place_id, place.pk)
        self.assertIsNotNone(location.place_resolved_at, "resolving onto a real place must be cached, or every page view re-runs it")

    def test_a_genuine_provider_miss_is_still_recorded_once(self) -> None:
        """The behaviour the stamp exists for, which must survive the fix.

        A coordinate the providers genuinely know nothing about is asked about
        once and then left alone - otherwise every page view re-runs the whole
        chain against an answer that will not change.
        """
        location = self._location()

        # Patched where the class is defined, not where it is used: provisioning
        # imports it inside the function body, so the module attribute this
        # replaces is the only one that exists at call time.
        with patch(
            "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
            return_value=ResolvedBoundaries(),
        ):
            self.assertIsNone(generate_location_boundaries(location))

        location.refresh_from_db()
        self.assertIsNotNone(location.place_resolved_at, "a chain run that found nothing must still record that we asked")
        self.assertTrue(boundary_generation_ran(location))


class ProviderGeometryOutranksOurOwnHullTests(TestCase):
    """A hull fitted around child pins yields to a parcel a provider supplied."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LON)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def _hull_row(self) -> Boundary:
        """The row ``refit_child_pin_boundary`` leaves behind, made directly.

        Built rather than provoked through the signal so the test states the
        state under test instead of depending on hierarchy-change plumbing.
        """
        return Boundary.objects.create(
            pin=self.pin,
            profile=self.profile,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square(CAMPUS_LON, CAMPUS_LAT, 0.004),
            generated_from_children=True,
        )

    def test_a_provider_parcel_replaces_the_child_fitted_hull(self) -> None:
        """The reported symptom, stated as the rule it breaks."""
        self._hull_row()
        parcel = official_geometry(self.location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01), kind=PlaceKind.PARCEL)
        self.pin.refresh_from_db()

        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)

        self.assertEqual(
            source,
            "place",
            "the map is still drawing the hull we fitted around this pin's children while a provider's parcel outline sits unused",
        )
        self.assertTrue(polygon.equals(parcel.geometry))

    def test_the_hull_still_applies_when_no_provider_has_offered_one(self) -> None:
        """The fallback has to survive: it is better than a 50 m circle."""
        self._hull_row()

        polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)

        self.assertEqual(source, "generated", "with no provider outline available the child-fitted hull is the best answer there is")
        self.assertIsNotNone(polygon)

    def test_a_drawing_of_the_users_own_still_wins(self) -> None:
        """Precedence changed for machine-fitted geometry only.

        A person who drew their own outline on this pin outranks every
        provider, and that must not have been disturbed.
        """
        row = self._hull_row()
        row.polygon = _square(CAMPUS_LON, CAMPUS_LAT, 0.002)
        row.save(update_fields=["polygon"])
        official_geometry(self.location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01))
        self.pin.refresh_from_db()

        _polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)

        self.assertEqual(source, "pin", "a user's own drawing was overridden by provider geometry")

    def test_geometry_we_did_not_fit_ourselves_keeps_its_precedence(self) -> None:
        """Only ``generated_from_children`` rows are stand-ins.

        A generated row that is not a hull around our own markers - the
        pre-places location default among them - is not this defect and keeps
        the precedence it had.
        """
        Boundary.objects.create(
            pin=self.pin,
            profile=self.profile,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square(CAMPUS_LON, CAMPUS_LAT, 0.004),
            generated_from_children=False,
        )
        official_geometry(self.location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01))
        self.pin.refresh_from_db()

        _polygon, source = Boundary.objects.resolve_for_pin(self.pin, BoundaryType.PROPERTY)

        self.assertEqual(source, "generated")


class ArrivingGeometrySupersedesTheStandInTests(TestCase):
    """The stand-in row is dropped once it can never be the best answer again."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LON)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        self.child_location = baker.make(Location, latitude=CAMPUS_LAT + 0.0004, longitude=CAMPUS_LON + 0.0004)
        self.child = baker.make(Pin, profile=self.profile, location=self.child_location, parent_pin=self.pin)

    def test_the_refit_drops_a_hull_that_a_parcel_has_superseded(self) -> None:
        """"Replace it right away" - not at the next cache expiry.

        The hull here is the one the child-pin signal fitted during setUp, not
        a constructed stand-in: this is the exact row the deployment had.
        """
        from urbanlens.dashboard.services.geo.child_pin_boundaries import refit_child_pin_boundary

        self.assertTrue(
            Boundary.objects.filter(pin=self.pin, generated_from_children=True).exists(),
            "attaching a child pin no longer fits a hull, so this test is no longer exercising the reported state",
        )
        official_geometry(self.location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01))
        self.pin.refresh_from_db()

        refit_child_pin_boundary(self.pin.pk)

        self.assertFalse(
            Boundary.objects.filter(pin=self.pin, generated_from_children=True).exists(),
            "a hull that can never be chosen again is still being maintained on every hierarchy change",
        )

    def test_no_hull_is_fitted_in_the_first_place_once_a_parcel_exists(self) -> None:
        """The cheaper half of the same rule."""
        from urbanlens.dashboard.services.geo.child_pin_boundaries import refit_child_pin_boundary

        official_geometry(self.location, _square(CAMPUS_LON, CAMPUS_LAT, 0.01))
        self.pin.refresh_from_db()

        refit_child_pin_boundary(self.pin.pk)

        self.assertFalse(
            Boundary.objects.filter(pin=self.pin, generated_from_children=True).exists(),
            "a stand-in was fitted for a pin that already has a provider's outline",
        )
