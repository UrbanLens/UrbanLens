"""Child pin/wiki slugs start with a short parent alias and drop whole words.

A building at Hudson River State Hospital should be ``hrsh-stafftenant-house-1900``,
not a mid-word clip of the building name. Prefix choice and word-boundary
truncation are pure functions (SimpleTestCase); minting on save is a TestCase.
"""

from __future__ import annotations

from django.utils.text import slugify
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import (
    PREFERRED_CHILD_SLUG_LENGTH,
    generate_short_prefix,
    is_uuid_slug,
    parent_slug_prefix,
    unique_slug,
)

_hyp = hyp_settings(max_examples=80, deadline=None)

_LONG_BUILDING = "Staff/Tenant House 1900 (non-contributing)"
_LONG_BUILDING_SPACED = "Staff/Tenant House 1900 (non contributing)"


class ParentSlugPrefixTests(SimpleTestCase):
    """Shortest compact alias wins; otherwise a prefix is derived from the long name."""

    def test_shortest_compact_alias_is_preferred(self) -> None:
        self.assertEqual(parent_slug_prefix(["Hudson River State Hospital", "HRSH"]), "hrsh")

    def test_the_shortest_of_several_compact_candidates_wins(self) -> None:
        # Both "HRSH" and "HRS" slugify short enough to lead a child slug;
        # only the actual shortest one should be picked, not just any of them.
        self.assertEqual(parent_slug_prefix(["Hudson River State Hospital", "HRSH", "HRS"]), "hrs")
        # Equal-length candidates break the tie toward fewer hyphens.
        self.assertEqual(parent_slug_prefix(["AB-CD", "ABCDE"]), "abcde")

    def test_long_name_without_alias_becomes_an_acronym(self) -> None:
        self.assertEqual(parent_slug_prefix(["Hudson River State Hospital"]), "hrsh")

    def test_initials_that_are_too_short_use_the_first_word(self) -> None:
        self.assertEqual(generate_short_prefix("Ford Motors"), "ford")

    def test_a_first_word_at_the_length_limit_is_kept_whole(self) -> None:
        # Exactly MAX_FIRST_WORD_LENGTH (10) chars - only one more and it truncates.
        self.assertEqual(generate_short_prefix("Powerhouse"), "powerhouse")

    def test_a_too_long_single_word_is_truncated(self) -> None:
        self.assertEqual(generate_short_prefix("Switzerland"), "switz")

    def test_articles_are_skipped_in_the_acronym(self) -> None:
        # Initials of "Hospital of the Hudson" are ``hh``, which is too short,
        # so the first significant word is used instead.
        self.assertEqual(generate_short_prefix("Hospital of the Hudson"), "hospital")
        self.assertEqual(parent_slug_prefix(["Hospital of the Hudson"]), "hospital")

    def test_a_name_with_no_usable_words_returns_empty(self) -> None:
        self.assertEqual(generate_short_prefix("..."), "")
        self.assertEqual(parent_slug_prefix(["", "   "]), "")


class WordBoundarySlugTests(SimpleTestCase):
    """Ideal slugs drop hyphenated compounds as a unit instead of clipping them."""

    def test_hyphenated_compound_is_dropped_as_one_word(self) -> None:
        slug = unique_slug(
            _LONG_BUILDING,
            is_taken=lambda _candidate: False,
            prefix="hrsh",
            max_length=255,
            preferred_length=PREFERRED_CHILD_SLUG_LENGTH,
        )
        self.assertEqual(slug, "hrsh-stafftenant-house-1900")
        self.assertNotIn("contributi", slug)

    def test_spaced_compound_keeps_a_whole_word_that_fits(self) -> None:
        slug = unique_slug(
            _LONG_BUILDING_SPACED,
            is_taken=lambda _candidate: False,
            prefix="hrsh",
            max_length=255,
            preferred_length=PREFERRED_CHILD_SLUG_LENGTH,
        )
        self.assertEqual(slug, "hrsh-stafftenant-house-1900-non")

    def test_collision_adds_back_the_dropped_compound(self) -> None:
        taken = {"hrsh-stafftenant-house-1900"}
        slug = unique_slug(
            _LONG_BUILDING,
            is_taken=taken.__contains__,
            prefix="hrsh",
            max_length=255,
            preferred_length=PREFERRED_CHILD_SLUG_LENGTH,
        )
        self.assertEqual(slug, "hrsh-stafftenant-house-1900-non-contributing")

    def test_short_ideal_grows_a_partial_word_when_taken(self) -> None:
        # "elm" alone is under MIN_SLUG_LENGTH and the next whole word doesn't
        # fit in the preferred budget, so a collision should grow it by taking
        # part of that next word rather than jumping straight to a numeric
        # suffix or a bare, still-too-short "elm".
        slug = unique_slug(
            "Elm Fieldhouse",
            is_taken=lambda candidate: candidate == "elm",
            max_length=40,
            preferred_length=10,
        )
        self.assertEqual(slug, "elm-fieldh")


class UniqueSlugPropertyTests(SimpleTestCase):
    """Invariants that hold for arbitrary names."""

    @given(name=st.text(min_size=0, max_size=120))
    @_hyp
    def test_never_exceeds_max_length_or_starts_with_a_hyphen(self, name: str) -> None:
        slug = unique_slug(name, is_taken=lambda _candidate: False, max_length=40, preferred_length=40)
        self.assertLessEqual(len(slug), 40)
        self.assertTrue(slug)
        self.assertFalse(slug.startswith("-"))
        self.assertFalse(slug.endswith("-"))
        # A generated slug is already in slug form - slugify is idempotent on it
        # unless the fallback is a raw UUID, which this path does not hit.
        self.assertEqual(slugify(slug), slug)

    def test_a_long_name_still_returns_promptly(self) -> None:
        """Leftover tokens that cannot fit must not spin the uniqueness loop."""
        slug = unique_slug(
            " ".join(["building"] * 40),
            is_taken=lambda _candidate: False,
            prefix="hrsh",
            max_length=40,
            preferred_length=40,
        )
        self.assertTrue(slug.startswith("hrsh-"))
        self.assertLessEqual(len(slug), 40)


class ChildPinSlugTests(TestCase):
    """Child pins mint a parent-prefixed slug on first save."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make("auth.User").profile
        self._seq = 0

    def _location(self) -> Location:
        self._seq += 1
        return Location.objects.create(latitude=41.7 + self._seq / 1000, longitude=-73.9 - self._seq / 1000)

    def _parent(self, name: str, *aliases: str) -> Pin:
        parent = Pin.objects.create(profile=self.profile, location=self._location(), name=name, parent_pin=None)
        for alias in aliases:
            PinAlias.objects.create(pin=parent, name=alias)
        return parent

    def test_root_pin_is_not_prefixed(self) -> None:
        pin = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name="Hudson River State Hospital",
            parent_pin=None,
        )
        self.assertEqual(pin.slug, "hudson-river-state-hospital")

    def test_child_uses_the_short_alias_as_a_prefix(self) -> None:
        parent = self._parent("Hudson River State Hospital", "HRSH")
        child = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name="Powerhouse",
            parent_pin=parent,
        )
        self.assertEqual(child.slug, "hrsh-powerhouse")

    def test_child_without_an_alias_still_gets_an_acronym_prefix(self) -> None:
        parent = self._parent("Hudson River State Hospital")
        child = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name="Powerhouse",
            parent_pin=parent,
        )
        self.assertEqual(child.slug, "hrsh-powerhouse")

    def test_hyphenated_building_name_is_not_clipped_mid_word(self) -> None:
        parent = self._parent("Hudson River State Hospital", "HRSH")
        child = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name=_LONG_BUILDING,
            parent_pin=parent,
        )
        self.assertEqual(child.slug, "hrsh-stafftenant-house-1900")

    def test_a_duplicate_child_name_reuses_the_dropped_compound(self) -> None:
        parent = self._parent("Hudson River State Hospital", "HRSH")
        first = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name=_LONG_BUILDING,
            parent_pin=parent,
        )
        second = Pin.objects.create(
            profile=self.profile,
            location=self._location(),
            name=_LONG_BUILDING,
            parent_pin=parent,
        )
        self.assertEqual(first.slug, "hrsh-stafftenant-house-1900")
        self.assertEqual(second.slug, "hrsh-stafftenant-house-1900-non-contributing")


class ChildWikiSlugTests(TestCase):
    """Child wikis mint the same prefixed slug, and copy it onto a UUID location slug."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")
        self._seq = 0

    def _location(self, **kwargs) -> Location:
        self._seq += 1
        return Location.objects.create(latitude=42.6 + self._seq / 1000, longitude=-73.7 - self._seq / 1000, **kwargs)

    def test_root_wiki_is_not_prefixed(self) -> None:
        wiki = Wiki.objects.create(location=self._location(), name="Hudson River State Hospital")
        self.assertEqual(wiki.slug, "hudson-river-state-hospital")

    def test_child_wiki_uses_the_parent_alias_as_a_prefix(self) -> None:
        parent = Wiki.objects.create(location=self._location(), name="Hudson River State Hospital")
        WikiAlias.objects.create(wiki=parent, name="HRSH")
        child = Wiki.objects.create(
            location=self._location(),
            name="Powerhouse",
            parent_wiki=parent,
        )
        self.assertEqual(child.slug, "hrsh-powerhouse")

    def test_uuid_location_slug_is_replaced_with_the_child_wiki_slug(self) -> None:
        parent = Wiki.objects.create(
            location=self._location(official_name="Hudson River State Hospital"), name="Hudson River State Hospital"
        )
        WikiAlias.objects.create(wiki=parent, name="HRSH")
        child_location = self._location()  # no official_name → UUID slug
        self.assertTrue(is_uuid_slug(child_location.slug))
        child = Wiki.objects.create(location=child_location, name="Powerhouse", parent_wiki=parent)
        child_location.refresh_from_db()
        self.assertEqual(child.slug, "hrsh-powerhouse")
        self.assertEqual(child_location.slug, "hrsh-powerhouse")

    def test_a_colliding_location_slug_is_left_alone(self) -> None:
        """The location copy is skipped when the wiki's own slug already belongs to another Location."""
        parent = Wiki.objects.create(location=self._location(), name="Hudson River State Hospital")
        WikiAlias.objects.create(wiki=parent, name="HRSH")
        Location.objects.create(latitude=10.0, longitude=10.0, slug="hrsh-powerhouse")
        child_location = self._location()  # no official_name → UUID slug
        original_slug = child_location.slug
        child = Wiki.objects.create(location=child_location, name="Powerhouse", parent_wiki=parent)
        child_location.refresh_from_db()
        self.assertEqual(child.slug, "hrsh-powerhouse")
        self.assertEqual(child_location.slug, original_slug)

    def test_a_named_location_slug_is_left_alone(self) -> None:
        parent = Wiki.objects.create(location=self._location(), name="Hudson River State Hospital")
        WikiAlias.objects.create(wiki=parent, name="HRSH")
        child_location = self._location(official_name="Powerhouse Building")
        original_slug = child_location.slug
        Wiki.objects.create(location=child_location, name="Powerhouse", parent_wiki=parent)
        child_location.refresh_from_db()
        self.assertEqual(child_location.slug, original_slug)
        self.assertEqual(original_slug, "powerhouse-building")
