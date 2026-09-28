"""A geolocation ping resolves every nearby pin's boundary in a fixed number of queries.

N29 G5-2/G5-5: each ping resolved a property polygon per nearby pin (a boundary query, a wiki lookup, a
wiki-boundary query, and a distance query when none applied), then updated each visited pin and its
Visited label one at a time. The batch resolver has to answer exactly what the per-pin resolver does, or
the query count is bought with wrong visits.
"""

from __future__ import annotations

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.visits.visits import record_geolocation_pin_visits


def _square(lng: float, lat: float, delta: float = 0.001) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def _pin(self, lat: float, lng: float, **extra) -> Pin:
        location = baker.make("dashboard.Location", latitude=f"{lat:.6f}", longitude=f"{lng:.6f}", **extra)
        return baker.make("dashboard.Pin", profile=self.profile, location=location)


class TheBatchAnswersWhatThePerPinResolverDoesTests(_Case):
    def test_every_branch_of_the_chain_agrees(self) -> None:
        own = self._pin(40.0, -74.0)
        baker.make(
            Boundary,
            pin=own,
            profile=self.profile,
            location=own.location,
            boundary_type=BoundaryType.PROPERTY,
            polygon=_square(-74.0, 40.0),
        )

        generated = self._pin(40.01, -74.0)
        baker.make(
            Boundary,
            pin=generated,
            profile=self.profile,
            location=generated.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=_square(-74.0, 40.01),
        )

        parcel = baker.make(Place, kind=PlaceKind.PARCEL, geometry=_square(-74.0, 40.02, 0.002))
        placed = self._pin(40.02, -74.0, place=parcel)

        linked_wiki_pin = self._pin(40.03, -74.0)
        linked_wiki = baker.make(
            "dashboard.Wiki", location=baker.make("dashboard.Location", latitude="40.030001", longitude="-74.000001")
        )
        baker.make(
            Boundary, wiki=linked_wiki, boundary_type=BoundaryType.PROPERTY, polygon=_square(-74.0, 40.03, 0.003)
        )
        Pin.objects.filter(pk=linked_wiki_pin.pk).update(wiki=linked_wiki)

        location_wiki_pin = self._pin(40.04, -74.0)
        location_wiki = baker.make("dashboard.Wiki", location=location_wiki_pin.location)
        baker.make(
            Boundary, wiki=location_wiki, boundary_type=BoundaryType.PROPERTY, polygon=_square(-74.0, 40.04, 0.004)
        )

        circle = self._pin(40.05, -74.0)

        child = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            parent_pin=own,
            location=baker.make("dashboard.Location", latitude="40.000100", longitude="-74.000100"),
        )

        pins = list(Pin.objects.filter(profile=self.profile).select_related("location"))
        batch = Boundary.objects.effective_polygons_for_pins(pins, BoundaryType.PROPERTY)

        for pin in pins:
            with self.subTest(pin=pin.pk):
                single = Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)
                self.assertEqual(batch[pin.pk] is None, single is None)
                if single is not None:
                    self.assertTrue(batch[pin.pk].equals_exact(single, tolerance=1e-9))
        self.assertTrue(
            {own.pk, generated.pk, placed.pk, linked_wiki_pin.pk, location_wiki_pin.pk, circle.pk, child.pk}
            <= set(batch)
        )


class APingCostsTheSameHoweverManyPinsAreNearbyTests(_Case):
    def _nearby(self, i: int) -> None:
        """A pin within 5 km that does not contain the point, resolving by one of four routes."""
        lat = 40.0 + i * 0.002
        kind = i % 4
        if kind == 0:
            pin = self._pin(lat, -74.0)
            baker.make(
                Boundary,
                pin=pin,
                profile=self.profile,
                location=pin.location,
                boundary_type=BoundaryType.PROPERTY,
                polygon=_square(-74.0, lat, 0.0005),
            )
        elif kind == 1:
            pin = self._pin(lat, -74.0)
            baker.make(
                Boundary,
                wiki=baker.make("dashboard.Wiki", location=pin.location),
                boundary_type=BoundaryType.PROPERTY,
                polygon=_square(-74.0, lat, 0.0005),
            )
        elif kind == 2:
            self._pin(lat, -74.0)  # falls through to the circle
        else:
            self._pin(lat, -74.0, place=baker.make(Place, kind=PlaceKind.BUILDING, geometry=None))  # no boundary at all

    def test_query_count_is_flat_in_nearby_pins(self) -> None:
        for i in range(1, 5):
            self._nearby(i)
        record_geolocation_pin_visits(
            self.profile, latitude=39.9998, longitude=-74.0002
        )  # PostGIS's one-off SRID lookup
        with CaptureQueriesContext(connection) as few:
            record_geolocation_pin_visits(self.profile, latitude=39.9998, longitude=-74.0002)

        for i in range(5, 21):
            self._nearby(i)
        with CaptureQueriesContext(connection) as many:
            visits = record_geolocation_pin_visits(self.profile, latitude=39.9998, longitude=-74.0002)

        self.assertEqual(visits, [])
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))
        self.assertGreater(len(few.captured_queries), 2, "the baseline must actually resolve boundaries")


class EveryContainingPinIsVisitedTests(_Case):
    def test_overlapping_pins_each_get_a_visit_their_date_and_the_visited_label(self) -> None:
        first = self._pin(40.0, -74.0)
        second = self._pin(40.0003, -74.0003)
        for pin in (first, second):
            baker.make(
                Boundary,
                pin=pin,
                profile=self.profile,
                location=pin.location,
                boundary_type=BoundaryType.PROPERTY,
                polygon=_square(-74.0, 40.0),
            )
        visited_label = Label.objects.filter(
            profile=self.profile, kind=KIND_STATUS, name="Visited"
        ).first() or baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Visited")
        when = timezone.now()

        visits = record_geolocation_pin_visits(self.profile, latitude=40.0001, longitude=-74.0001, visited_at=when)

        self.assertEqual(sorted(visit.pin_id for visit in visits), sorted([first.pk, second.pk]))
        self.assertEqual(PinVisit.objects.filter(source=VisitSource.GEOLOCATION).count(), 2)
        for pin in (first, second):
            pin.refresh_from_db()
            self.assertEqual(pin.last_visited, when)
            self.assertTrue(pin.labels.filter(pk=visited_label.pk).exists())

    def test_a_pin_with_no_boundary_counts_only_near_its_marker(self) -> None:
        parcel = baker.make(Place, kind=PlaceKind.BUILDING, geometry=None)
        near = self._pin(40.0, -74.0, place=parcel)

        self.assertEqual(
            Boundary.objects.effective_polygon_for_pin(near, BoundaryType.PROPERTY),
            None,
            "the fixture must reach the marker fallback",
        )
        self.assertEqual(
            [v.pin_id for v in record_geolocation_pin_visits(self.profile, latitude=40.0002, longitude=-74.0002)],
            [near.pk],
        )
        PinVisit.objects.all().delete()
        self.assertEqual(record_geolocation_pin_visits(self.profile, latitude=40.01, longitude=-74.0), [])


class TheBatchLabelWriteStillCascadesTests(_Case):
    def test_a_detail_pin_marked_in_a_batch_passes_visited_up_to_its_parent(self) -> None:
        from urbanlens.dashboard.services.visits.visits import _add_visited_status_to

        parent = self._pin(40.0, -74.0)
        child = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            parent_pin=parent,
            location=baker.make("dashboard.Location", latitude="40.000200", longitude="-74.000200"),
        )
        root = self._pin(40.01, -74.0)
        visited = Label.objects.filter(profile=self.profile, kind=KIND_STATUS, name="Visited").first() or baker.make(
            Label, profile=self.profile, kind=KIND_STATUS, name="Visited"
        )

        _add_visited_status_to(self.profile, [child, root])

        for pin in (child, parent, root):
            with self.subTest(pin=pin.pk):
                self.assertTrue(pin.labels.filter(pk=visited.pk).exists())
