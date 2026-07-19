"""Tests for the custom-field REFERENCE picker's "wiki" kind recognizing
boundary-mate wikis (docs/PROBLEMS.md follow-up).

Before this fix, referenceable_queryset("wiki", profile) used
`Wiki.objects.filter(location__pins__profile=profile)` - an exact-Location-row-
only check that duplicated (and never got updated to match) the boundary-mate
fix already applied to wiki_access.location_visible_to in commit 15e6e2e2. A
user whose pin sits on the same building as an existing wiki - but at a
boundary-mate Location row, not the wiki's own exact Location - could already
see and open that wiki (the earlier fix), but still could not select it as a
REFERENCE custom field target. See wiki_access.visible_wiki_location_ids.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.custom_field_references import referenceable_queryset, resolve_reference


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class ReferenceableWikiQuerysetBoundaryMateTests(TestCase):
    """A pin at a boundary-mate Location makes that wiki referenceable too, not just an exact match."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        Boundary.objects.create(location=self.wiki_location, generated_polygon=_square(-74.0, 40.0, 0.003))
        self.wiki = baker.make(Wiki, location=self.wiki_location)

    def test_exact_location_pin_wiki_is_referenceable(self) -> None:
        baker.make(Pin, profile=self.profile, location=self.wiki_location)

        self.assertIn(self.wiki, referenceable_queryset("wiki", self.profile))

    def test_no_pin_anywhere_wiki_is_not_referenceable(self) -> None:
        self.assertNotIn(self.wiki, referenceable_queryset("wiki", self.profile))

    def test_boundary_mate_pin_wiki_is_referenceable(self) -> None:
        """The regression this fix closes: a pin at a distinct Location row
        whose point still falls inside the wiki location's own boundary."""
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        self.assertNotEqual(nearby_location.pk, self.wiki_location.pk)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        self.assertIn(self.wiki, referenceable_queryset("wiki", self.profile))

    def test_pin_far_outside_the_boundary_is_not_referenceable(self) -> None:
        far_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        baker.make(Pin, profile=self.profile, location=far_location)

        self.assertNotIn(self.wiki, referenceable_queryset("wiki", self.profile))

    def test_another_profiles_boundary_mate_pin_does_not_grant_referenceability(self) -> None:
        other = baker.make(User).profile
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        baker.make(Pin, profile=other, location=nearby_location)

        self.assertNotIn(self.wiki, referenceable_queryset("wiki", self.profile))

    def test_resolve_reference_also_recognizes_the_boundary_mate(self) -> None:
        nearby_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        baker.make(Pin, profile=self.profile, location=nearby_location)

        resolved = resolve_reference("wiki", self.wiki.pk, self.profile)

        self.assertEqual(resolved, self.wiki)
