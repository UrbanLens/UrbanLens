"""Tests for the global search engine: scoping, typo tolerance, NL filters, history."""

from __future__ import annotations

from typing import ClassVar

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.search_history import SearchHistory
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.global_search.providers import SearchProvider


class PinSearchTests(TestCase):
    """Pins: own-content scoping, typo tolerance, and place filtering."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.other_user = baker.make("auth.User")
        self.other_profile = self.other_user.profile

        self.location = baker.make(
            "dashboard.Location",
            latitude="39.10", longitude="-84.51",
            locality="Cincinnati", administrative_area_level_1="OH",
        )
        self.pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=self.location,
            name="Willow Grove Mill",
            description="Rusty turbines in the basement",
        )
        self.other_pin = baker.make(
            "dashboard.Pin",
            profile=self.other_profile,
            name="Willow Grove Mill",
        )

    def _titles(self, response, slug="pins"):
        for group in response.groups:
            if group.meta.slug == slug:
                return [result.title for result in group.results]
        return []

    def test_finds_own_pin_by_name(self):
        response = GlobalSearchEngine().search(self.profile, "willow grove")
        self.assertIn("Willow Grove Mill", self._titles(response))

    def test_does_not_return_other_users_pins(self):
        response = GlobalSearchEngine().search(self.other_profile, "willow grove")
        titles = self._titles(response)
        self.assertEqual(titles.count("Willow Grove Mill"), 1)

    def test_typo_tolerant_name_match(self):
        response = GlobalSearchEngine().search(self.profile, "wilow grove mil")
        self.assertIn("Willow Grove Mill", self._titles(response))

    def test_matches_description_text(self):
        response = GlobalSearchEngine().search(self.profile, "rusty turbines")
        self.assertIn("Willow Grove Mill", self._titles(response))

    def test_place_filter_pins_in_city(self):
        baker.make("dashboard.Pin", profile=self.profile, name="Elsewhere Spot")
        response = GlobalSearchEngine().search(self.profile, "pins in Cincinnati")
        titles = self._titles(response)
        self.assertIn("Willow Grove Mill", titles)
        self.assertNotIn("Elsewhere Spot", titles)

    def test_type_filter_excludes_other_sections(self):
        response = GlobalSearchEngine().search(self.profile, "willow pins")
        self.assertTrue(all(group.meta.slug == "pins" for group in response.groups))

    def test_fallback_when_structured_parse_matches_nothing(self):
        # "in the mill" parses as a place filter, which matches no address -
        # the engine must fall back to plain text and still find the pin.
        stairs = baker.make("dashboard.Pin", profile=self.profile, name="Stairs in the Mill")
        response = GlobalSearchEngine().search(self.profile, "stairs in the mill")
        self.assertTrue(response.used_fallback or "Stairs in the Mill" in self._titles(response))
        found = [result.title for group in response.groups for result in group.results]
        self.assertIn(stairs.name, found)

    def test_short_query_returns_nothing(self):
        response = GlobalSearchEngine().search(self.profile, "w")
        self.assertEqual(response.total, 0)

    def test_near_me_filters_to_pins_close_to_the_profile(self):
        self.profile.map_custom_latitude = "39.10"
        self.profile.map_custom_longitude = "-84.51"
        self.profile.save()
        far_location = baker.make("dashboard.Location", latitude="51.50", longitude="-0.12")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location, name="London Fog Tower")
        response = GlobalSearchEngine().search(self.profile, "pins near me")
        titles = self._titles(response)
        self.assertIn("Willow Grove Mill", titles)
        self.assertNotIn("London Fog Tower", titles)

    def test_near_me_without_known_location_does_not_error(self):
        stranger = baker.make("auth.User").profile
        baker.make("dashboard.Pin", profile=stranger, name="Somewhere Spot")
        response = GlobalSearchEngine().search(stranger, "pins near me")
        self.assertEqual(response.errors, [])

    def test_near_me_with_terms_does_not_drop_distant_text_match(self):
        # A pin literally named "Church Near Me" must be found even though
        # it isn't actually close to the profile - once there's a term to
        # match on its own, distance is a ranking signal, not a filter.
        self.profile.map_custom_latitude = "39.10"
        self.profile.map_custom_longitude = "-84.51"
        self.profile.save()
        far_location = baker.make("dashboard.Location", latitude="51.50", longitude="-0.12")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location, name="Church Near Me")
        response = GlobalSearchEngine().search(self.profile, "church near me")
        self.assertIn("Church Near Me", self._titles(response))

    def test_near_me_with_terms_ranks_nearby_match_first(self):
        self.profile.map_custom_latitude = "39.10"
        self.profile.map_custom_longitude = "-84.51"
        self.profile.save()
        nearby_location = baker.make("dashboard.Location", latitude="39.11", longitude="-84.50")
        baker.make("dashboard.Pin", profile=self.profile, location=nearby_location, name="Old Church")
        far_location = baker.make("dashboard.Location", latitude="51.50", longitude="-0.12")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location, name="Church Near Me")
        response = GlobalSearchEngine().search(self.profile, "church near me")
        titles = self._titles(response)
        self.assertIn("Old Church", titles)
        self.assertIn("Church Near Me", titles)
        self.assertLess(titles.index("Old Church"), titles.index("Church Near Me"))
        
    def test_multiple_suggestions_all_returned(self):
        self.profile.map_custom_latitude = "39.10"
        self.profile.map_custom_longitude = "-84.51"
        self.profile.save()
        nearby_location = baker.make("dashboard.Location", latitude="39.11", longitude="-84.50")
        baker.make("dashboard.Pin", profile=self.profile, location=nearby_location, name="Old factory in PA")
        far_location = baker.make("dashboard.Location", latitude="51.50", longitude="-0.11")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location, name="old factory that's Near Me")
        far_location2 = baker.make("dashboard.Location", latitude="52.50", longitude="-0.12")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location2, name="messages from Sarah")
        far_location3 = baker.make("dashboard.Location", latitude="53.50", longitude="-0.12")
        baker.make("dashboard.Pin", profile=self.profile, location=far_location3, name="church far away")
        
        terms = ["factory near me", "old factory"]
        for term in terms:
            response = GlobalSearchEngine().search(self.profile, term)
            titles = self._titles(response)
            self.assertIn("Old factory in PA", titles)
            self.assertIn("old factory that's Near Me", titles)
            self.assertNotIn("messages from Sarah", titles)
            self.assertNotIn("church far away", titles)
            
        response = GlobalSearchEngine().search(self.profile, "messages from Sarah")
        titles = self._titles(response)
        self.assertIn("messages from Sarah", titles)
        self.assertNotIn("old factory in PA", titles)
        self.assertNotIn("old factory that's Near Me", titles)
        self.assertNotIn("church far away", titles)


class PhotoSearchTests(TestCase):
    """Photos: caption/keyword matching and uploader scoping."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.image = baker.make(
            "dashboard.Image",
            profile=self.profile,
            caption="Sunset over the graffiti hall",
            _create_files=True,
        )
        baker.make("dashboard.ImageKeyword", image=self.image, source="test", keyword="staircase")

    def _photo_titles(self, response):
        for group in response.groups:
            if group.meta.slug == "photos":
                return [result.title for result in group.results]
        return []

    def test_finds_photo_by_caption(self):
        response = GlobalSearchEngine().search(self.profile, "graffiti hall")
        self.assertIn("Sunset over the graffiti hall", self._photo_titles(response))

    def test_finds_photo_by_generated_keyword(self):
        response = GlobalSearchEngine().search(self.profile, "staircase photos")
        self.assertIn("Sunset over the graffiti hall", self._photo_titles(response))

    def test_other_user_cannot_see_unrelated_photo(self):
        stranger = baker.make("auth.User").profile
        response = GlobalSearchEngine().search(stranger, "graffiti hall")
        self.assertEqual(self._photo_titles(response), [])


class DirectMessageSearchTests(TestCase):
    """Messages: participant scoping and encrypted bodies staying unsearchable."""

    def setUp(self):
        self.alice = baker.make("auth.User", username="alice").profile
        self.bob = baker.make("auth.User", username="bob").profile
        self.eve = baker.make("auth.User", username="eve").profile
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="Meet at the old asylum gate")
        baker.make("dashboard.DirectMessage", sender=self.alice, recipient=self.bob, body="", ciphertext="deadbeef", nonce="abc")

    def _message_results(self, response):
        for group in response.groups:
            if group.meta.slug == "messages":
                return group.results
        return []

    def test_participant_finds_message(self):
        for profile in (self.alice, self.bob):
            response = GlobalSearchEngine().search(profile, "asylum gate")
            self.assertEqual(len(self._message_results(response)), 1)

    def test_non_participant_finds_nothing(self):
        response = GlobalSearchEngine().search(self.eve, "asylum gate")
        self.assertEqual(self._message_results(response), [])

    def test_encrypted_message_not_searchable(self):
        response = GlobalSearchEngine().search(self.alice, "deadbeef")
        self.assertEqual(self._message_results(response), [])

    def test_messages_from_person_finds_conversation_without_text_terms(self):
        response = GlobalSearchEngine().search(self.bob, f"messages from {self.alice.username}")
        results = self._message_results(response)
        self.assertEqual(len(results), 1)
        self.assertIn(self.alice.username, results[0].title)

    def test_messages_from_person_excludes_other_conversations(self):
        baker.make("dashboard.DirectMessage", sender=self.eve, recipient=self.bob, body="Unrelated chat")
        response = GlobalSearchEngine().search(self.bob, f"messages from {self.alice.username}")
        results = self._message_results(response)
        self.assertTrue(all(self.alice.username in result.title for result in results))


class _ExplodingProvider(SearchProvider):
    """Provider that always fails, for error-isolation tests."""

    slug = "pins"
    fuzzy_field = ""

    def search(self, profile, parsed, limit):
        raise RuntimeError("boom")


class EngineErrorHandlingTests(TestCase):
    """One failing provider must not break the search."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_provider_failure_becomes_error_notice(self):
        engine = GlobalSearchEngine(providers=[_ExplodingProvider()])
        response = engine.search(self.profile, "anything at all")
        self.assertEqual(response.total, 0)
        self.assertTrue(response.errors)
        self.assertIn("Pins", response.errors[0])


class SearchHistoryTests(TestCase):
    """Recent-search recording: dedupe, bumping, and pruning."""

    prune_patch: ClassVar = None

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_record_deduplicates_and_bumps_use_count(self):
        first = SearchHistory.objects.record(self.profile, "old mill")
        second = SearchHistory.objects.record(self.profile, "  old   mill ")
        self.assertIsNotNone(first)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.use_count, 2)
        self.assertEqual(SearchHistory.objects.for_profile(self.profile).count(), 1)

    def test_blank_query_not_recorded(self):
        self.assertIsNone(SearchHistory.objects.record(self.profile, "   "))
        self.assertEqual(SearchHistory.objects.for_profile(self.profile).count(), 0)

    def test_history_pruned_to_cap(self):
        from unittest.mock import patch

        with patch("urbanlens.dashboard.models.search_history.queryset.MAX_HISTORY_PER_PROFILE", 5):
            for index in range(8):
                SearchHistory.objects.record(self.profile, f"query {index}")
        self.assertLessEqual(SearchHistory.objects.for_profile(self.profile).count(), 5)
        # The most recent query survives pruning.
        surviving = {row.query for row in SearchHistory.objects.for_profile(self.profile)}
        self.assertIn("query 7", surviving)

    def test_recent_for_orders_most_recent_first(self):
        SearchHistory.objects.record(self.profile, "first")
        SearchHistory.objects.record(self.profile, "second")
        SearchHistory.objects.record(self.profile, "first")
        recent = SearchHistory.objects.recent_for(self.profile)
        self.assertEqual(recent[0].query, "first")
