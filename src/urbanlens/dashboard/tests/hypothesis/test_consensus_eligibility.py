"""Tests for Consensus wiki eligibility (services.consensus.eligibility).

Only wikis whose Location the requesting profile has a *visited* pin for
are ever offered as rounds - not merely pinned, per the Consensus design
spec (stricter than SpotGuessr's "pinned by everyone" rule).
"""

from __future__ import annotations

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.eligibility import eligible_wikis, eligible_wikis_for_all, has_eligible_wikis, has_eligible_wikis_for_all


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class EligibleWikisTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()

    def test_wiki_with_no_pin_at_all_is_not_eligible(self) -> None:
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        self.assertFalse(eligible_wikis(self.profile).exists())

    def test_wiki_with_an_unvisited_pin_is_not_eligible(self) -> None:
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location, last_visited=None)
        self.assertFalse(eligible_wikis(self.profile).exists())

    def test_wiki_with_a_visited_pin_is_eligible(self) -> None:
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location, last_visited=timezone.now())
        self.assertIn(wiki, list(eligible_wikis(self.profile)))

    def test_another_profiles_visited_pin_does_not_make_it_eligible_for_me(self) -> None:
        other_profile = _make_profile()
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        baker.make(Pin, profile=other_profile, location=location, last_visited=timezone.now())
        self.assertFalse(eligible_wikis(self.profile).exists())

    def test_exclude_wiki_ids_removes_a_wiki_that_would_otherwise_be_eligible(self) -> None:
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location, last_visited=timezone.now())
        self.assertFalse(eligible_wikis(self.profile, exclude_wiki_ids=[wiki.pk]).exists())

    def test_has_eligible_wikis_matches_eligible_wikis_exists(self) -> None:
        self.assertFalse(has_eligible_wikis(self.profile))
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location, last_visited=timezone.now())
        self.assertTrue(has_eligible_wikis(self.profile))


class EligibleWikisForAllTests(TestCase):
    def test_empty_profile_list_returns_nothing(self) -> None:
        self.assertFalse(eligible_wikis_for_all([]).exists())
        self.assertFalse(has_eligible_wikis_for_all([]))

    def test_requires_every_profile_to_have_visited_it(self) -> None:
        alice = _make_profile()
        bob = _make_profile()
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=alice, location=location, last_visited=timezone.now())
        # Bob hasn't pinned it at all yet.
        self.assertFalse(eligible_wikis_for_all([alice, bob]).exists())

        baker.make(Pin, profile=bob, location=location, last_visited=timezone.now())
        self.assertIn(wiki, list(eligible_wikis_for_all([alice, bob])))
