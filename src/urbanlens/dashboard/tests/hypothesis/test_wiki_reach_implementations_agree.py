"""One rule for wiki reach, two implementations, and nothing binding them together.

``location_visible_to(location, profile)`` answers "can this viewer reach this
one wiki". ``visible_wiki_location_ids(profile)`` builds the whole set the viewer
can reach, and its own docstring calls itself "the set-shaped counterpart" to the
first. They are separate code paths - one walks the viewer's pins for a single
location, the other resolves the reachable set database-side - and the property
that makes either safe to substitute for the other has never been asserted:

    location_visible_to(loc, viewer) == (loc.pk in visible_wiki_location_ids(viewer))

Worth binding for two reasons. The immediate one is H44: the single-location form
answers in one query where the set form takes four, and the media path authorizes
exactly one image, so the cheap form is the remaining win there - but only if it
cannot disagree. The larger one is that two implementations of a *visibility* rule
drifting apart is a disclosure bug that no test would catch, and the domain rules
they encode (a pin on a different Location row of the same parcel still counts,
aggregates earn their members) are exactly the kind that get changed in one place.

The scenarios below are the ones where the two could plausibly diverge: an exact
pin, a boundary mate on the same parcel, a place with no pin at all, and a viewer
with no pins whatsoever.

**The equivalence has a precondition, and writing this found it.** The set form
filters `wiki__isnull=False` - it lists wikis that *exist* - while
`location_visible_to` answers "may you reach a wiki here" whether or not one has
been created. For a location with no wiki they disagree by construction, and that
is not a defect: there is nothing to show. So the substitution is only sound where
a wiki exists, which is exactly the media path's case (a photo is attached to
one). `TheEquivalenceNeedsAWikiToExistTests` pins that boundary, so the next
person to reach for the cheap form knows what it assumes.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import resolution
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to, visible_wiki_location_ids

from .test_places_campus import make_place, square as _square


class _ReachCase(TestCase):
    """A parcel, a wiki location on it, a boundary mate, and somewhere far away."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = baker.make(User).profile

        self.parcel = make_place(PlaceKind.PARCEL, _square(-74.0, 40.0, 0.003), name="Reach Parcel")
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0, official_name="Reach Spot")
        resolution.resolve_location_place(self.wiki_location)

        self.mate_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        resolution.resolve_location_place(self.mate_location)

        self.far_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        resolution.resolve_location_place(self.far_location)

        # Every location under test carries a wiki: the set form reports only
        # locations that have one, so without this the two forms are being asked
        # different questions rather than compared.
        for location in (self.wiki_location, self.mate_location, self.far_location):
            baker.make(Wiki, location=location)

    def assertImplementationsAgree(self, *locations: Location) -> None:
        """The two reach implementations must answer identically for each location."""
        viewer = Profile.objects.get(pk=self.viewer.pk)
        reachable = visible_wiki_location_ids(viewer)
        for location in locations:
            with self.subTest(location=location.pk):
                # A fresh instance per call so neither answer is served from the
                # other's per-instance memo.
                single = location_visible_to(
                    Location.objects.get(pk=location.pk), Profile.objects.get(pk=self.viewer.pk)
                )
                self.assertEqual(
                    single,
                    location.pk in reachable,
                    f"location_visible_to said {single} while the set form said {location.pk in reachable}",
                )


class WithNoPinsAtAllTests(_ReachCase):
    def test_they_agree_that_nothing_is_reachable(self) -> None:
        self.assertImplementationsAgree(self.wiki_location, self.mate_location, self.far_location)


class WithAnExactPinTests(_ReachCase):
    def test_they_agree_on_the_pinned_location_and_on_the_others(self) -> None:
        baker.make(Pin, profile=self.viewer, location=self.wiki_location)

        self.assertImplementationsAgree(self.wiki_location, self.mate_location, self.far_location)


class WithABoundaryMatePinTests(_ReachCase):
    """A pin on a *different* Location row of the same parcel still earns the wiki."""

    def test_they_agree_that_the_mate_pin_reaches_the_wiki_location(self) -> None:
        baker.make(Pin, profile=self.viewer, location=self.mate_location)

        self.assertImplementationsAgree(self.wiki_location, self.mate_location, self.far_location)

    def test_and_that_it_does_not_reach_somewhere_else(self) -> None:
        """The anti-vacuity half: agreeing on "everything is visible" would be no test."""
        baker.make(Pin, profile=self.viewer, location=self.mate_location)
        viewer = Profile.objects.get(pk=self.viewer.pk)

        self.assertNotIn(self.far_location.pk, visible_wiki_location_ids(viewer))
        self.assertFalse(
            location_visible_to(Location.objects.get(pk=self.far_location.pk), Profile.objects.get(pk=self.viewer.pk))
        )


class WithAPinOnlyFarAwayTests(_ReachCase):
    def test_they_agree_the_parcel_is_still_out_of_reach(self) -> None:
        baker.make(Pin, profile=self.viewer, location=self.far_location)

        self.assertImplementationsAgree(self.wiki_location, self.mate_location, self.far_location)


class TheEquivalenceNeedsAWikiToExistTests(_ReachCase):
    """The precondition the substitution rests on, asserted rather than assumed."""

    def setUp(self) -> None:
        super().setUp()
        self.wikiless = Location.objects.create(latitude=40.0008, longitude=-74.0008)
        resolution.resolve_location_place(self.wikiless)
        baker.make(Pin, profile=self.viewer, location=self.wiki_location)

    def test_the_single_form_answers_for_a_location_with_no_wiki(self) -> None:
        """ "May you reach a wiki here" has an answer even where none was created."""
        self.assertTrue(
            location_visible_to(Location.objects.get(pk=self.wikiless.pk), Profile.objects.get(pk=self.viewer.pk))
        )

    def test_the_set_form_does_not_list_it(self) -> None:
        """It lists wikis that exist, so a location with none is absent by construction."""
        self.assertNotIn(self.wikiless.pk, visible_wiki_location_ids(Profile.objects.get(pk=self.viewer.pk)))

    def test_but_they_still_agree_once_a_wiki_is_created_there(self) -> None:
        """Which is what makes the substitution sound wherever a photo is attached."""
        baker.make(Wiki, location=self.wikiless)

        self.assertImplementationsAgree(self.wikiless)
