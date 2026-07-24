"""Tests for public-pin voting (UL-58): eligibility engine, vote lifecycle, endpoint, suggestions."""

from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_suggestions import _pending_suggestions
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.links.model import WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus, PublicPinVote
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.public_pins import (
    PublicPinConfig,
    PublicVoteError,
    cast_public_vote,
    evaluate_public_pin_candidates,
    is_meaningful_name,
    pinned_by_floor,
    public_vote_context,
    sync_public_pin_suggestions,
)

#: min_open_days=0 so pass conditions can be evaluated without backdating.
FAST_CONFIG = PublicPinConfig(min_open_days=0)


def _make_profiles(count: int) -> list[Profile]:
    """Create ``count`` users and return their auto-created profiles."""
    return [Profile.objects.get(user=baker.make("auth.User")) for _ in range(count)]


def _make_eligible_location(
    *,
    latitude: str = "42.650000",
    longitude: str = "-73.760000",
    state: str = "NY",
    name: str = "Old Mill Sanatorium",
    pinners: int = 2,
) -> tuple[Location, Wiki, list[Profile]]:
    """Build a location that passes every eligibility criterion at defaults.

    Returns the location, its wiki, and the pin-holding profiles (who are
    also the vulnerability voters).
    """
    location = baker.make(Location, latitude=latitude, longitude=longitude, administrative_area_level_1=state)
    wiki = baker.make(Wiki, location=location, name=name)
    profiles = _make_profiles(max(pinners, 3))
    for profile in profiles[:pinners]:
        baker.make(Pin, profile=profile, location=location)
    for profile in profiles[:3]:
        baker.make(WikiStatVote, wiki=wiki, profile=profile, field=WikiStatField.VULNERABILITY, value=1)
    baker.make(WikiAlias, wiki=wiki, name="The Mill")
    baker.make(WikiLink, wiki=wiki, name="History", url="https://example.com/history")
    baker.make(WikiLink, wiki=wiki, name="Photos", url="https://example.com/photos")
    baker.make(Image, wiki=wiki)
    baker.make(Image, wiki=wiki)
    baker.make(Article, wiki=wiki, content="x" * 300)
    baker.make(
        PinMarkup,
        parent_wiki=wiki,
        profile=profiles[0],
        markup_type="line",
        geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
    )
    return location, wiki, profiles[:pinners]


class PurePublicPinRuleTests(TestCase):
    """Unit tests for the pure rule helpers."""

    def test_pinned_by_floor_scales_and_clamps(self) -> None:
        self.assertEqual(pinned_by_floor(0), 2)
        self.assertEqual(pinned_by_floor(10), 2)
        self.assertEqual(pinned_by_floor(20), 4)
        self.assertEqual(pinned_by_floor(26), 6)
        self.assertEqual(pinned_by_floor(1000), 10)

    def test_meaningful_name_heuristics(self) -> None:
        for bad in ("", "   ", "abc", "40.71, -74.00", "N 40 E 74", "Untitled", "unknown"):
            with self.subTest(name=bad):
                self.assertFalse(is_meaningful_name(bad))
        for good in ("Mill", "Old Mill Sanatorium", "Bldg 12 Annex"):
            with self.subTest(name=good):
                self.assertTrue(is_meaningful_name(good))


class EligibilityEngineTests(TestCase):
    """The periodic engine opens/suspends/reopens candidates correctly."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin

    def test_fully_eligible_location_opens_a_vote(self) -> None:
        location, _wiki, _profiles = _make_eligible_location()
        counters = evaluate_public_pin_candidates()
        self.assertEqual(counters["opened"], 1)
        candidate = PublicPinCandidate.objects.get(location=location)
        self.assertEqual(candidate.status, PublicPinCandidateStatus.OPEN)

    def test_each_missing_criterion_blocks_eligibility(self) -> None:
        cases = {
            "no_alias": lambda w: w.aliases.all().delete(),
            "one_link": lambda w: w.links.first().delete(),
            "one_photo": lambda w: w.images.first().delete(),
            "short_article": lambda w: Article.objects.filter(wiki=w).update(content="too short"),
            "no_markup": lambda w: w.markup_items.all().delete(),
            "placeholder_name": lambda w: Wiki.objects.filter(pk=w.pk).update(name="Untitled"),
            "high_vulnerability": lambda w: WikiStatVote.objects.filter(wiki=w).update(value=4),
            "too_few_vuln_votes": lambda w: w.stat_votes.first().delete(),
            "no_state": lambda w: Location.objects.filter(pk=w.location_id).update(administrative_area_level_1=""),
        }
        for index, (label, break_it) in enumerate(cases.items()):
            with self.subTest(criterion=label):
                lat = f"41.{index:02d}0000"
                location, wiki, _ = _make_eligible_location(latitude=lat, longitude="-72.500000")
                break_it(wiki)
                evaluate_public_pin_candidates()
                self.assertFalse(
                    PublicPinCandidate.objects.filter(location=location).exists(),
                    f"{label} should have blocked eligibility",
                )

    def test_too_few_pinners_blocks_eligibility(self) -> None:
        location, _wiki, profiles = _make_eligible_location(pinners=2)
        Pin.objects.filter(profile=profiles[1], location=location).delete()
        evaluate_public_pin_candidates()
        self.assertFalse(PublicPinCandidate.objects.filter(location=location).exists())

    def test_lapse_suspends_and_recovery_reopens(self) -> None:
        location, wiki, _ = _make_eligible_location()
        evaluate_public_pin_candidates()
        link = wiki.links.first()
        link.delete()
        counters = evaluate_public_pin_candidates()
        self.assertEqual(counters["suspended"], 1)
        candidate = PublicPinCandidate.objects.get(location=location)
        self.assertEqual(candidate.status, PublicPinCandidateStatus.SUSPENDED)

        baker.make(WikiLink, wiki=wiki, name="Restored", url="https://example.com/again")
        counters = evaluate_public_pin_candidates()
        self.assertEqual(counters["reopened"], 1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PublicPinCandidateStatus.OPEN)

    def test_top_n_per_state_cuts_the_least_pinned(self) -> None:
        config = PublicPinConfig(top_n_per_state=2)
        _make_eligible_location(latitude="42.100000", pinners=4)
        _make_eligible_location(latitude="42.200000", pinners=3)
        low_location, _, _ = _make_eligible_location(latitude="42.300000", pinners=2)
        evaluate_public_pin_candidates(config)
        self.assertEqual(PublicPinCandidate.objects.count(), 2)
        self.assertFalse(PublicPinCandidate.objects.filter(location=low_location).exists())

    def test_region_exclusivity_blocks_nearby_after_a_pass(self) -> None:
        location, _wiki, pinners = _make_eligible_location()
        evaluate_public_pin_candidates(FAST_CONFIG)
        for profile in pinners:
            cast_public_vote(location, profile, "public")
        evaluate_public_pin_candidates(FAST_CONFIG)
        self.assertEqual(PublicPinCandidate.objects.get(location=location).status, PublicPinCandidateStatus.PASSED)

        # ~5 km north: blocked. ~55 km north: eligible again.
        near, _, _ = _make_eligible_location(latitude="42.695000", name="Near Neighbor Works")
        far, _, _ = _make_eligible_location(latitude="43.150000", name="Far Foundry Complex")
        evaluate_public_pin_candidates(FAST_CONFIG)
        self.assertFalse(PublicPinCandidate.objects.filter(location=near).exists())
        self.assertTrue(PublicPinCandidate.objects.filter(location=far).exists())


class VoteLifecycleTests(TestCase):
    """Pass/fail settlement rules."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location, self.wiki, self.pinners = _make_eligible_location()
        evaluate_public_pin_candidates()
        self.candidate = PublicPinCandidate.objects.get(location=self.location)

    def test_passes_with_consensus_after_minimum_open_time(self) -> None:
        for profile in self.pinners:
            cast_public_vote(self.location, profile, "public")
        PublicPinCandidate.objects.filter(pk=self.candidate.pk).update(opened_at=timezone.now() - timedelta(days=8))
        counters = evaluate_public_pin_candidates()
        self.assertEqual(counters["passed"], 1)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, PublicPinCandidateStatus.PASSED)
        self.assertIsNotNone(self.candidate.decided_at)

    def test_does_not_pass_before_minimum_open_time(self) -> None:
        for profile in self.pinners:
            cast_public_vote(self.location, profile, "public")
        counters = evaluate_public_pin_candidates()
        self.assertEqual(counters["passed"], 0)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, PublicPinCandidateStatus.OPEN)

    def test_does_not_pass_without_consensus(self) -> None:
        cast_public_vote(self.location, self.pinners[0], "public")
        cast_public_vote(self.location, self.pinners[1], "private")
        counters = evaluate_public_pin_candidates(FAST_CONFIG)
        self.assertEqual(counters["passed"], 0)

    def test_hard_fail_rejects_permanently(self) -> None:
        config = PublicPinConfig(fail_min_votes=2)
        for profile in self.pinners:
            baker.make(PublicPinVote, candidate=self.candidate, profile=profile, make_public=False)
        evaluate_public_pin_candidates(config)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, PublicPinCandidateStatus.REJECTED)

        # A rejected location never becomes a candidate again.
        evaluate_public_pin_candidates(config)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, PublicPinCandidateStatus.REJECTED)
        self.assertEqual(PublicPinCandidate.objects.filter(location=self.location).count(), 1)

    def test_suspended_candidates_never_settle(self) -> None:
        for profile in self.pinners:
            cast_public_vote(self.location, profile, "public")
        PublicPinCandidate.objects.filter(pk=self.candidate.pk).update(status=PublicPinCandidateStatus.SUSPENDED)
        self.wiki.links.first().delete()  # keep it ineligible so it stays suspended
        counters = evaluate_public_pin_candidates(FAST_CONFIG)
        self.assertEqual(counters["passed"], 0)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, PublicPinCandidateStatus.SUSPENDED)

    def test_cast_requires_open_candidate_and_pin(self) -> None:
        outsider = Profile.objects.get(user=baker.make("auth.User"))
        with pytest.raises(PublicVoteError):
            cast_public_vote(self.location, outsider, "public")

        PublicPinCandidate.objects.filter(pk=self.candidate.pk).update(status=PublicPinCandidateStatus.SUSPENDED)
        with pytest.raises(PublicVoteError):
            cast_public_vote(self.location, self.pinners[0], "public")

    def test_withdraw_removes_the_ballot(self) -> None:
        cast_public_vote(self.location, self.pinners[0], "public")
        self.assertEqual(PublicPinVote.objects.count(), 1)
        cast_public_vote(self.location, self.pinners[0], "withdraw")
        self.assertEqual(PublicPinVote.objects.count(), 0)


class PublicVoteEndpointTests(TestCase):
    """The HTMX endpoint and the context builder behind the block."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location, self.wiki, self.pinners = _make_eligible_location()
        evaluate_public_pin_candidates()
        self.voter = self.pinners[0]
        self.client.force_login(self.voter.user)
        self.url = reverse("location.wiki.public_vote", args=[self.location.slug])

    def test_context_hides_block_from_non_pinners(self) -> None:
        outsider = Profile.objects.get(user=baker.make("auth.User"))
        self.assertIsNone(public_vote_context(self.location, outsider))
        context = public_vote_context(self.location, self.voter)
        self.assertIsNotNone(context)
        self.assertIsNone(context["my_vote"])

    def test_context_shows_badge_when_public(self) -> None:
        PublicPinCandidate.objects.filter(location=self.location).update(status=PublicPinCandidateStatus.PASSED)
        outsider = Profile.objects.get(user=baker.make("auth.User"))
        self.assertEqual(public_vote_context(self.location, outsider), {"is_public": True})

    def test_post_records_a_ballot_and_rerenders(self) -> None:
        response = self.client.post(self.url, {"choice": "public"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You voted to make this location public")
        vote = PublicPinVote.objects.get(profile=self.voter)
        self.assertTrue(vote.make_public)

    def test_post_change_and_withdraw(self) -> None:
        self.client.post(self.url, {"choice": "public"})
        response = self.client.post(self.url, {"choice": "private"})
        self.assertContains(response, "keep this location private")
        self.assertFalse(PublicPinVote.objects.get(profile=self.voter).make_public)

        response = self.client.post(self.url, {"choice": "withdraw"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Should this location be visible to every UrbanLens user?")
        self.assertEqual(PublicPinVote.objects.count(), 0)

    def test_post_rejects_bad_choice(self) -> None:
        response = self.client.post(self.url, {"choice": "maybe"})
        self.assertEqual(response.status_code, 400)

    def test_never_exposes_a_tally(self) -> None:
        cast_public_vote(self.location, self.pinners[1], "public")
        response = self.client.post(self.url, {"choice": "public"})
        content = response.content.decode()
        self.assertNotIn("vote" + "s", content.lower())  # no "2 votes" style counts
        self.assertNotIn("tally", content.lower())


class SuggestionSyncTests(TestCase):
    """Public locations fan out as opt-out suggestions."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location, self.wiki, self.pinners = _make_eligible_location()
        evaluate_public_pin_candidates()
        PublicPinCandidate.objects.filter(location=self.location).update(status=PublicPinCandidateStatus.PASSED)

    def test_sync_targets_only_opted_in_profiles_without_the_pin(self) -> None:
        recipient = Profile.objects.get(user=baker.make("auth.User"))
        opted_out = Profile.objects.get(user=baker.make("auth.User"))
        opted_out.suggest_public_pins = False
        opted_out.save(update_fields=["suggest_public_pins"])
        no_community = Profile.objects.get(user=baker.make("auth.User"))
        no_community.community_enabled = False
        no_community.save(update_fields=["community_enabled"])

        created = sync_public_pin_suggestions()
        suggestions = PinSuggestion.objects.filter(origin=PinSuggestionOrigin.COMMUNITY)
        recipient_ids = set(suggestions.values_list("profile_id", flat=True))

        self.assertIn(recipient.pk, recipient_ids)
        self.assertNotIn(opted_out.pk, recipient_ids)
        self.assertNotIn(no_community.pk, recipient_ids)
        for pinner in self.pinners:
            self.assertNotIn(pinner.pk, recipient_ids)
        self.assertEqual(created, suggestions.count())

        suggestion = suggestions.get(profile=recipient)
        self.assertEqual(suggestion.location_id, self.location.pk)
        self.assertEqual(suggestion.suggested_name, self.wiki.name)

        # Idempotent: a second run creates nothing new.
        self.assertEqual(sync_public_pin_suggestions(), 0)

    def test_queue_hides_community_suggestions_when_toggled_off(self) -> None:
        recipient = Profile.objects.get(user=baker.make("auth.User"))
        sync_public_pin_suggestions()
        self.assertEqual(_pending_suggestions(recipient).count(), 1)

        recipient.suggest_public_pins = False
        recipient.save(update_fields=["suggest_public_pins"])
        self.assertEqual(_pending_suggestions(recipient).count(), 0)

        # Hidden, not deleted: toggling back on restores it.
        recipient.suggest_public_pins = True
        recipient.save(update_fields=["suggest_public_pins"])
        self.assertEqual(_pending_suggestions(recipient).count(), 1)
