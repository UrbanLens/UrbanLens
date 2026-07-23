"""Tests for plugin-driven place-name resolution.

Covers the address-derived quality gate, the rule-based resolver (agreement >
priority > arrival order), the plugin-fed candidate pipeline, and the
"current name always has an alias row" invariant on Pin and Wiki saves.
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.locations.name_resolution import (
    NameCandidate,
    NameProvider,
    RuleBasedNameResolver,
    default_name_resolver,
)
from urbanlens.dashboard.services.locations.naming import (
    external_name_candidates_for_location,
    is_address_derived_name,
    update_location_name_from_external_sources,
)

_hyp = hyp_settings(max_examples=60, deadline=None)


def _location(**kwargs) -> Location:
    """Unsaved Location with address components; the gate never touches the DB."""
    return Location(latitude="0", longitude="0", **kwargs)


class _StaticProvider(NameProvider):
    """Name provider returning a fixed candidate list, for pipeline tests."""

    def __init__(self, source: str, names: list) -> None:
        super().__init__(source=source)
        self._names = list(names)

    def candidates(self, location) -> list:
        return self._names


class _BrokenProvider(NameProvider):
    """Name provider that always raises, for isolation tests."""

    def candidates(self, location) -> list:
        raise RuntimeError("broken provider")


def _patch_providers(*providers: NameProvider):
    """Replace the plugin registry's name providers for the duration of a test."""
    return patch(
        "urbanlens.dashboard.plugins.registry.PluginRegistry.name_providers",
        return_value=list(providers),
    )


# -- Address-derived quality gate --------------------------------------------------


class IsAddressDerivedNameTests(SimpleTestCase):
    """Street/city/state fragments must not be promoted to official names."""

    def test_street_name_reported_as_place_name_is_rejected(self) -> None:
        loc = _location(street_number="2663", route="Westwood Northern Blvd", locality="Cincinnati", administrative_area_level_1="OH")
        self.assertTrue(is_address_derived_name("Westwood Northern Blvd", loc))

    def test_city_reported_as_place_name_is_rejected(self) -> None:
        loc = _location(street_number="451", route="Southern Blvd", locality="Albany", administrative_area_level_1="NY")
        self.assertTrue(is_address_derived_name("Albany", loc))

    def test_state_reported_as_place_name_is_rejected(self) -> None:
        loc = _location(locality="Poughkeepsie", administrative_area_level_1="New York")
        self.assertTrue(is_address_derived_name("New York", loc))

    def test_place_the_street_was_named_after_is_kept(self) -> None:
        # "Kenwood" appears in "1 Kenwood Road" but carries no street-type word:
        # the street was named after the place, so the name is real.
        loc = _location(street_number="1", route="Kenwood Road", locality="Albany", administrative_area_level_1="NY")
        self.assertFalse(is_address_derived_name("Kenwood", loc))

    def test_real_place_name_is_kept(self) -> None:
        loc = _location(street_number="10", route="Ship St", locality="Poughkeepsie", administrative_area_level_1="NY")
        self.assertFalse(is_address_derived_name("Hudson River State Hospital", loc))

    def test_street_abbreviations_count_as_street_words(self) -> None:
        loc = _location(street_number="12", route="Miller Rd", locality="Utica", administrative_area_level_1="NY")
        self.assertTrue(is_address_derived_name("Miller Rd", loc))

    def test_street_word_not_matching_address_is_kept(self) -> None:
        loc = _location(street_number="9", route="Main St", locality="Springfield")
        self.assertFalse(is_address_derived_name("Electric Avenue", loc))

    def test_blank_components_never_match(self) -> None:
        loc = _location()
        self.assertFalse(is_address_derived_name("Westwood Northern Blvd", loc))

    @given(
        transform=st.sampled_from([str.upper, str.lower, str.title]),
        pad=st.sampled_from(["", " ", ".", ", "]),
    )
    @_hyp
    def test_case_and_punctuation_noise_does_not_change_verdicts(self, transform, pad: str) -> None:
        reject_loc = _location(street_number="2663", route="Westwood Northern Blvd", locality="Cincinnati", administrative_area_level_1="OH")
        self.assertTrue(is_address_derived_name(transform("Westwood Northern Blvd") + pad, reject_loc))
        keep_loc = _location(street_number="1", route="Kenwood Road", locality="Albany", administrative_area_level_1="NY")
        self.assertFalse(is_address_derived_name(transform("Kenwood") + pad, keep_loc))


class IsAddressDerivedNameFuzzyVariantsTests(SimpleTestCase):
    """Real-world address variants (off-by-one house numbers, ranged/block addresses,
    abbreviated vs. full street suffixes and state names) must still be recognized
    as address-derived, not just an exact/near-exact match of the stored fields."""

    def _amsterdam_location(self) -> Location:
        return _location(street_number="1050", route="Main St", locality="Amsterdam", administrative_area_level_1="NY")

    def test_off_by_one_house_number_is_still_address_derived(self) -> None:
        self.assertTrue(is_address_derived_name("1051 Main St", self._amsterdam_location()))

    def test_off_by_one_house_number_with_full_address_text(self) -> None:
        self.assertTrue(is_address_derived_name("1051 main st, amsterdam, ny", self._amsterdam_location()))

    def test_ranged_block_address_containing_the_house_number(self) -> None:
        self.assertTrue(is_address_derived_name("1030-1060 Main St, Amsterdam, NY", self._amsterdam_location()))

    def test_full_street_suffix_and_full_state_name_and_zip(self) -> None:
        self.assertTrue(is_address_derived_name("1049 Main Street, Amsterdam, New York, 12010", self._amsterdam_location()))

    def test_house_number_far_outside_tolerance_is_not_matched(self) -> None:
        """A house number too far off to plausibly be the same building/block must still be kept."""
        self.assertFalse(is_address_derived_name("2200 Main St", self._amsterdam_location()))

    def test_ranged_address_not_containing_the_house_number_is_not_matched(self) -> None:
        self.assertFalse(is_address_derived_name("2000-2010 Main St", self._amsterdam_location()))

    def test_matching_number_but_different_street_is_not_matched(self) -> None:
        self.assertFalse(is_address_derived_name("1050 Elm St", self._amsterdam_location()))

    def test_full_state_name_alone_matches_stored_abbreviation(self) -> None:
        loc = _location(locality="Poughkeepsie", administrative_area_level_1="NY")
        self.assertTrue(is_address_derived_name("New York", loc))

    def test_abbreviation_alone_matches_stored_full_state_name(self) -> None:
        loc = _location(locality="Poughkeepsie", administrative_area_level_1="New York")
        self.assertTrue(is_address_derived_name("NY", loc))

    def test_abbreviated_route_matches_full_suffix_candidate(self) -> None:
        loc = _location(street_number="12", route="Miller Rd", locality="Utica", administrative_area_level_1="NY")
        self.assertTrue(is_address_derived_name("Miller Road", loc))

    def test_kenwood_still_kept_even_with_a_ranged_or_fuzzy_number(self) -> None:
        """Regression guard: the new house-number tolerance path must not
        accidentally reopen the "place the street was named after" exception -
        Kenwood carries no street-type word, so it's rejected before house
        numbers are even considered."""
        loc = _location(street_number="1", route="Kenwood Road", locality="Albany", administrative_area_level_1="NY")
        self.assertFalse(is_address_derived_name("1 Kenwood", loc))


# -- RuleBasedNameResolver ----------------------------------------------------------


class RuleBasedNameResolverTests(SimpleTestCase):
    """Two-source agreement beats priority; priority beats arrival order."""

    def _resolve(self, candidates, priority=()):
        return RuleBasedNameResolver(priority).resolve(candidates, _location())

    def test_agreement_beats_a_higher_priority_lone_source(self) -> None:
        candidates = [
            NameCandidate(name="Solo Name", source="google_places"),
            NameCandidate(name="Agreed Name", source="wikipedia"),
            NameCandidate(name="AGREED-name!", source="nps"),
        ]
        winner = self._resolve(candidates, ["google_places", "wikipedia", "nps"])
        self.assertEqual(winner, NameCandidate(name="Agreed Name", source="wikipedia"))

    def test_priority_orders_lone_sources(self) -> None:
        candidates = [
            NameCandidate(name="From NPS", source="nps"),
            NameCandidate(name="From Wikipedia", source="wikipedia"),
        ]
        winner = self._resolve(candidates, ["wikipedia", "nps"])
        self.assertEqual(winner.name, "From Wikipedia")

    def test_unlisted_sources_rank_after_listed_ones(self) -> None:
        candidates = [
            NameCandidate(name="Mystery", source="mystery_source"),
            NameCandidate(name="Known", source="nps"),
        ]
        self.assertEqual(self._resolve(candidates, ["nps"]).name, "Known")

    def test_unlisted_sources_fall_back_to_arrival_order(self) -> None:
        candidates = [
            NameCandidate(name="First", source="src_a"),
            NameCandidate(name="Second", source="src_b"),
        ]
        self.assertEqual(self._resolve(candidates).name, "First")

    def test_agreement_surface_form_comes_from_highest_priority_source(self) -> None:
        candidates = [
            NameCandidate(name="old mill", source="nps"),
            NameCandidate(name="Old Mill", source="wikipedia"),
        ]
        winner = self._resolve(candidates, ["wikipedia", "nps"])
        self.assertEqual(winner.name, "Old Mill")

    def test_empty_candidates_resolve_to_none(self) -> None:
        self.assertIsNone(self._resolve([]))

    @given(names=st.lists(st.sampled_from(["Mill", "Bridge", "Tower"]), min_size=1, max_size=6))
    @_hyp
    def test_winner_is_always_one_of_the_candidates(self, names: list[str]) -> None:
        candidates = [NameCandidate(name=name, source=f"src{index}") for index, name in enumerate(names)]
        self.assertIn(self._resolve(candidates, ["src0"]), candidates)


class RuleBasedNameResolverOverrideSourceTests(SimpleTestCase):
    """override_source wins outright, even against a two-source agreement."""

    def test_override_source_wins_against_agreement(self) -> None:
        candidates = [
            NameCandidate(name="Agreed Name", source="wikipedia"),
            NameCandidate(name="AGREED-name!", source="nps"),
            NameCandidate(name="REData Name", source="redata_building"),
        ]
        resolver = RuleBasedNameResolver(["wikipedia", "nps"], override_source="redata_building")
        self.assertEqual(resolver.resolve(candidates, _location()).name, "REData Name")

    def test_override_source_absent_falls_back_to_normal_ranking(self) -> None:
        candidates = [
            NameCandidate(name="Agreed Name", source="wikipedia"),
            NameCandidate(name="AGREED-name!", source="nps"),
        ]
        resolver = RuleBasedNameResolver(["wikipedia", "nps"], override_source="redata_building")
        self.assertEqual(resolver.resolve(candidates, _location()).name, "Agreed Name")

    def test_no_override_source_configured_is_a_no_op(self) -> None:
        candidates = [NameCandidate(name="Only", source="wikipedia")]
        resolver = RuleBasedNameResolver([])
        self.assertEqual(resolver.resolve(candidates, _location()).name, "Only")


class DefaultNameResolverChildPinOverrideTests(TestCase):
    """default_name_resolver only activates the REData-building override for a child pin's location."""

    def _make_location_with_pin(self, *, parent_pin=None) -> Location:
        profile = baker.make("dashboard.Profile")
        location = baker.make(Location, latitude="42.65", longitude="-73.75", google_place=None)
        baker.make(Pin, profile=profile, location=location, parent_pin=parent_pin)
        return location

    def test_no_override_for_a_root_pins_location(self) -> None:
        location = self._make_location_with_pin(parent_pin=None)
        resolver = default_name_resolver(location=location)
        candidates = [NameCandidate(name="Agreed", source="wikipedia"), NameCandidate(name="AGREED!", source="nps"), NameCandidate(name="REData", source="redata_building")]
        self.assertEqual(resolver.resolve(candidates, location).name, "Agreed")

    def test_override_for_a_child_pins_location(self) -> None:
        profile = baker.make("dashboard.Profile")
        parent_location = baker.make(Location, latitude="42.00", longitude="-73.00", google_place=None)
        parent = baker.make(Pin, profile=profile, location=parent_location, parent_pin=None)
        location = self._make_location_with_pin(parent_pin=parent)
        resolver = default_name_resolver(location=location)
        candidates = [NameCandidate(name="Agreed", source="wikipedia"), NameCandidate(name="AGREED!", source="nps"), NameCandidate(name="REData", source="redata_building")]
        self.assertEqual(resolver.resolve(candidates, location).name, "REData")

    def test_no_location_given_is_a_no_op(self) -> None:
        resolver = default_name_resolver()
        candidates = [NameCandidate(name="Agreed", source="wikipedia"), NameCandidate(name="AGREED!", source="nps"), NameCandidate(name="REData", source="redata_building")]
        self.assertEqual(resolver.resolve(candidates, _location()).name, "Agreed")

    def test_unsaved_location_is_a_no_op(self) -> None:
        resolver = default_name_resolver(location=_location())
        candidates = [NameCandidate(name="Only", source="redata_building")]
        # An unsaved (no pk) location can't have any pins - override_source
        # stays unset, so this still resolves via the normal ranking, not a
        # crash from querying an unsaved instance's `.pins`.
        self.assertEqual(resolver.resolve(candidates, _location()).name, "Only")


class NameSourcePriorityListTests(SimpleTestCase):
    """SiteSettings parses the comma-separated priority into a clean slug list."""

    def test_parses_and_strips_slugs(self) -> None:
        settings = SiteSettings(default_name_source_priority=" google_places ,wikipedia,, nps ")
        self.assertEqual(settings.name_source_priority_list, ["google_places", "wikipedia", "nps"])

    def test_blank_priority_is_empty(self) -> None:
        self.assertEqual(SiteSettings(default_name_source_priority="").name_source_priority_list, [])


# -- Candidate pipeline --------------------------------------------------------------


class ExternalNameCandidatesTests(TestCase):
    """Candidates come from plugin providers, cleaned and quality-gated."""

    def test_candidates_come_from_plugin_providers(self) -> None:
        loc = baker.make(Location, latitude="41.100000", longitude="-73.100000")
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            candidates = external_name_candidates_for_location(loc)
        self.assertEqual(candidates, [NameCandidate(name="Old Mill", source="wikipedia")])

    def test_address_derived_candidates_are_filtered(self) -> None:
        loc = baker.make(
            Location,
            latitude="39.150000",
            longitude="-84.610000",
            street_number="2663",
            route="Westwood Northern Blvd",
            locality="Cincinnati",
            administrative_area_level_1="OH",
        )
        with _patch_providers(_StaticProvider("google_places", ["Westwood Northern Blvd", "Cincinnati", "Real Museum"])):
            candidates = external_name_candidates_for_location(loc)
        self.assertEqual([candidate.name for candidate in candidates], ["Real Museum"])

    def test_meaningless_and_duplicate_candidates_are_dropped(self) -> None:
        loc = baker.make(Location, latitude="41.110000", longitude="-73.110000")
        with _patch_providers(_StaticProvider("wikipedia", ["Dropped Pin", None, "Old Mill", "old-MILL"])):
            candidates = external_name_candidates_for_location(loc)
        self.assertEqual(candidates, [NameCandidate(name="Old Mill", source="wikipedia")])

    def test_broken_provider_is_isolated(self) -> None:
        loc = baker.make(Location, latitude="41.120000", longitude="-73.120000")
        with (
            _patch_providers(_BrokenProvider(source="broken"), _StaticProvider("nps", ["Park Name"])),
            self.assertLogs("urbanlens.dashboard.services.locations.naming", level="ERROR"),
        ):
            candidates = external_name_candidates_for_location(loc)
        self.assertEqual(candidates, [NameCandidate(name="Park Name", source="nps")])

    def test_extra_candidates_come_before_plugin_candidates(self) -> None:
        loc = baker.make(Location, latitude="41.130000", longitude="-73.130000")
        with _patch_providers(_StaticProvider("nps", ["Park Name"])):
            candidates = external_name_candidates_for_location(loc, extra_candidates=[("fresh_source", "Fresh Name")])
        self.assertEqual([candidate.source for candidate in candidates], ["fresh_source", "nps"])


class UpdateLocationNameResolutionTests(TestCase):
    """The resolver drives official_name; candidates persist as official aliases."""

    def _location_with_wiki(self, *, wiki_name: str, lat: str, lng: str):
        loc = baker.make(Location, latitude=lat, longitude=lng)
        wiki = baker.make("dashboard.Wiki", location=loc, name=wiki_name)
        return loc, wiki

    def test_official_aliases_are_recorded_with_kind_and_source(self) -> None:
        loc, wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.200000", lng="-73.200000")
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            self.assertTrue(update_location_name_from_external_sources(loc))
        alias = wiki.aliases.get(name="Old Mill")
        self.assertEqual(alias.kind, "official")
        self.assertEqual(alias.source, "wikipedia")

    def test_pin_at_the_location_also_receives_an_official_alias(self) -> None:
        """Regression guard: name providers used to populate WikiAlias only,
        never PinAlias, despite update_location_name_from_external_sources'
        own docstring claiming otherwise for both."""
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.201000", lng="-73.201000")
        pin: Pin = baker.make(Pin, profile=baker.make("dashboard.Profile"), location=loc)
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            self.assertTrue(update_location_name_from_external_sources(loc))
        alias = pin.aliases.get(name="Old Mill")
        self.assertEqual(alias.kind, "official")
        self.assertEqual(alias.source, "wikipedia")

    def test_every_pin_at_a_shared_location_gets_the_alias(self) -> None:
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.202000", lng="-73.202000")
        pin_a: Pin = baker.make(Pin, profile=baker.make("dashboard.Profile"), location=loc)
        pin_b: Pin = baker.make(Pin, profile=baker.make("dashboard.Profile"), location=loc)
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            update_location_name_from_external_sources(loc)
        self.assertTrue(pin_a.aliases.filter(name="Old Mill").exists())
        self.assertTrue(pin_b.aliases.filter(name="Old Mill").exists())

    def test_no_pins_at_the_location_is_not_an_error(self) -> None:
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.203000", lng="-73.203000")
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            self.assertTrue(update_location_name_from_external_sources(loc))

    def test_existing_pin_alias_is_left_untouched(self) -> None:
        """A user-created alias with the same name must not be overwritten - the
        provider-sourced row is only created when no row already exists."""
        from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias

        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.204000", lng="-73.204000")
        pin: Pin = baker.make(Pin, profile=baker.make("dashboard.Profile"), location=loc)
        PinAlias.objects.create(pin=pin, name="Old Mill", kind=AliasType.NICKNAME, source="user")
        with _patch_providers(_StaticProvider("wikipedia", ["Old Mill"])):
            update_location_name_from_external_sources(loc)
        alias = pin.aliases.get(name="Old Mill")
        self.assertEqual(alias.kind, "nickname")
        self.assertEqual(alias.source, "user")

    def test_agreement_between_sources_beats_default_priority(self) -> None:
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.210000", lng="-73.210000")
        with _patch_providers(
            _StaticProvider("google_places", ["Solo Hall"]),
            _StaticProvider("wikipedia", ["Agreed Hall"]),
            _StaticProvider("nps", ["agreed hall"]),
        ):
            update_location_name_from_external_sources(loc)
        loc.refresh_from_db()
        self.assertEqual(loc.official_name, "Agreed Hall")

    def test_admin_priority_orders_lone_sources(self) -> None:
        settings = SiteSettings.get_current()
        settings.default_name_source_priority = "nps,wikipedia"
        settings.save(update_fields=["default_name_source_priority", "updated"])
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.220000", lng="-73.220000")
        with _patch_providers(
            _StaticProvider("wikipedia", ["Wiki Name"]),
            _StaticProvider("nps", ["Park Name"]),
        ):
            update_location_name_from_external_sources(loc)
        loc.refresh_from_db()
        self.assertEqual(loc.official_name, "Park Name")

    def test_google_places_is_dropped_when_another_source_has_a_candidate(self) -> None:
        """Google Places is demoted to fallback-only: any other source's candidate wins outright."""
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.230000", lng="-73.230000")
        with _patch_providers(
            _StaticProvider("google_places", ["Noisy Google Name"]),
            _StaticProvider("nps", ["Park Name"]),
        ):
            update_location_name_from_external_sources(loc)
        loc.refresh_from_db()
        self.assertEqual(loc.official_name, "Park Name")

    def test_google_places_is_used_when_it_is_the_only_source(self) -> None:
        loc, _wiki = self._location_with_wiki(wiki_name="Curated Mill", lat="41.240000", lng="-73.240000")
        with _patch_providers(_StaticProvider("google_places", ["Only Google Name"])):
            update_location_name_from_external_sources(loc)
        loc.refresh_from_db()
        self.assertEqual(loc.official_name, "Only Google Name")


# -- Current-name alias invariant ----------------------------------------------------


class PinNameAliasInvariantTests(TestCase):
    """Every meaningful Pin name that gets persisted has an alias row."""

    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile

    def test_creating_named_pin_records_alias(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)
        self.assertEqual(list(pin.aliases.values_list("name", flat=True)), ["Old Factory"])

    def test_renaming_pin_keeps_old_and_new_names_as_aliases(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)
        pin.name = "New Factory"
        pin.save(update_fields=["name", "updated"])
        self.assertCountEqual(list(pin.aliases.values_list("name", flat=True)), ["Old Factory", "New Factory"])

    def test_rest_serializer_rename_records_alias(self) -> None:
        from urbanlens.dashboard.models.pin.serializer import PinSerializer

        pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)
        PinSerializer().update(pin, {"name": "Renamed Depot"})
        pin.refresh_from_db()
        self.assertEqual(pin.name, "Renamed Depot")
        self.assertIn("Renamed Depot", list(pin.aliases.values_list("name", flat=True)))

    def test_meaningless_names_do_not_create_aliases(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Dropped Pin")
        self.assertEqual(pin.aliases.count(), 0)

    def test_save_without_name_in_update_fields_is_ignored(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)
        pin.aliases.all().delete()
        pin.priority = 3
        pin.save(update_fields=["priority", "updated"])
        self.assertEqual(pin.aliases.count(), 0)


# -- Name-source priority picker UI --------------------------------------------------


class NameSourcePriorityPickerRenderTests(TestCase):
    """The site-admin picker is the only one left - users can no longer override
    name-source priority themselves, see naming.py's _FALLBACK_ONLY_SOURCES and
    default_name_resolver's docstring."""

    def test_settings_page_no_longer_renders_a_user_priority_picker(self) -> None:
        from django.test import Client
        from django.urls import reverse

        user = baker.make("auth.User")
        client = Client()
        client.force_login(user)
        html = client.get(reverse("settings.view")).content.decode()
        self.assertNotIn("user-name-source-priority-list", html)
        self.assertNotIn('name="name_source_priority"', html)

    def test_site_admin_page_renders_default_priority_picker(self) -> None:
        from django.test import Client
        from django.urls import reverse

        user = baker.make("auth.User", is_superuser=True, is_staff=True)
        client = Client()
        client.force_login(user)
        html = client.get(reverse("site_admin")).content.decode()
        self.assertIn('id="name-source-priority-list"', html)
        self.assertIn('id="default-name-source-priority"', html)
        self.assertIn('name="default_name_source_priority"', html)


class WikiNameAliasInvariantTests(TestCase):
    """Every meaningful Wiki name that gets persisted has an alias row."""

    def test_creating_named_wiki_records_alias(self) -> None:
        loc = baker.make(Location, latitude="41.300000", longitude="-73.300000")
        wiki = baker.make("dashboard.Wiki", location=loc, name="Curated Mill")
        self.assertEqual(list(wiki.aliases.values_list("name", flat=True)), ["Curated Mill"])

    def test_renaming_wiki_keeps_old_and_new_names_as_aliases(self) -> None:
        loc = baker.make(Location, latitude="41.310000", longitude="-73.310000")
        wiki = baker.make("dashboard.Wiki", location=loc, name="Curated Mill")
        wiki.name = "Restored Mill"
        wiki.save(update_fields=["name", "updated"])
        self.assertCountEqual(list(wiki.aliases.values_list("name", flat=True)), ["Curated Mill", "Restored Mill"])
