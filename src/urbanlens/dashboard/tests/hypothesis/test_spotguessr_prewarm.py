"""Tests for services.spotguessr.prewarm and its session.py/tasks.py integration.

See docs: the SpotGuessr /start/ slowness fix - selection.py's N+1 fix
addresses the round trip's own dominant cost, and this background prewarm
addresses what's left (mainly Street View mode's live Google Maps lookup -
services.spotguessr.street_view) by running the next round's selection
ahead of time and caching the result, so the request that actually needs it
can consume a cache hit instead of generating live.
"""

from __future__ import annotations

from itertools import count
from unittest.mock import patch

from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSessionStatus, SpotGuessrMode
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr import prewarm
from urbanlens.dashboard.services.spotguessr.session import (
    GameConfig,
    begin_session,
    generate_round_content,
    get_or_create_round,
    join_session,
    start_multiplayer_session,
    start_solo_session,
    submit_guess,
)
from urbanlens.dashboard.tasks import prewarm_spotguessr_round, prewarm_spotguessr_solo_start

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


def _befriend(a: Profile, b: Profile) -> None:
    friendship = Friendship.request(a, b)
    assert friendship is not None
    friendship.accept()


def _make_photo_location(profile: Profile) -> Location:
    location = _make_location()
    baker.make(Pin, profile=profile, location=location)
    baker.make(Image, location=location, media_type=MediaKind.PHOTO, wiki=baker.make(Wiki, location=location))
    return location


class SessionScopedCacheRoundtripTests(TestCase):
    def test_store_then_consume_returns_the_cached_pick(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        picked = generate_round_content(SpotGuessrMode.PHOTOS, GameConfig(), [profile], [], None)
        assert picked is not None
        _, content = picked

        prewarm.store_for_session(999999, 1, location, content)
        result = prewarm.consume_for_session(999999, 1)

        assert result is not None
        self.assertEqual(result[0], location.pk)
        # Popped, not just peeked - a second consume must miss.
        self.assertIsNone(prewarm.consume_for_session(999999, 1))

    def test_consume_without_a_prior_store_is_a_clean_miss(self) -> None:
        self.assertIsNone(prewarm.consume_for_session(123456789, 0))


class SoloStartCacheRoundtripTests(TestCase):
    def test_store_then_consume_with_the_same_config_hits(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        config = GameConfig(difficulty=0.7)
        picked = generate_round_content(SpotGuessrMode.PHOTOS, config, [profile], [], None)
        assert picked is not None
        _, content = picked

        prewarm.store_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config, location, content)
        result = prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config)

        assert result is not None
        self.assertEqual(result[0], location.pk)

    def test_a_different_config_misses(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        stored_config = GameConfig(difficulty=0.7)
        picked = generate_round_content(SpotGuessrMode.PHOTOS, stored_config, [profile], [], None)
        assert picked is not None
        _, content = picked
        prewarm.store_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, stored_config, location, content)

        different_config = GameConfig(difficulty=0.1)
        self.assertIsNone(prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, different_config))
        # The mismatched lookup didn't consume the real entry - clean it up.
        self.assertIsNotNone(prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, stored_config))

    def test_a_different_mode_misses(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        config = GameConfig()
        picked = generate_round_content(SpotGuessrMode.PHOTOS, config, [profile], [], None)
        assert picked is not None
        _, content = picked
        prewarm.store_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config, location, content)

        self.assertIsNone(prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.NAMED_PLACE, config))
        self.assertIsNotNone(prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config))


class GetOrCreateRoundConsumesSessionPrewarmTests(TestCase):
    def test_a_cached_session_pick_is_used_instead_of_live_generation(self) -> None:
        profile = _make_profile()
        # Two eligible locations, but only one gets prewarmed - if the round
        # is genuinely served from cache (not a lucky random match), it must
        # be exactly that one.
        cached_location = _make_photo_location(profile)
        _make_photo_location(profile)

        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)
        # This round is made the session's *last* one (bypassing the
        # MIN_ROUNDS_PER_SESSION=3 floor start_solo_session's own
        # total_rounds param would clamp to) so creating it doesn't also
        # enqueue - and eagerly run - a prewarm for a round after it, which
        # would call the mocked generate_round_content too and confuse the
        # "never called live" assertion below with an unrelated call.
        session.total_rounds = 1
        session.save(update_fields=["total_rounds"])
        picked = generate_round_content(SpotGuessrMode.PHOTOS, GameConfig(), [profile], [], None)
        assert picked is not None
        _, content = picked
        prewarm.store_for_session(session.pk, 0, cached_location, content)

        with patch("urbanlens.dashboard.services.spotguessr.session.generate_round_content") as mock_generate:
            round_ = get_or_create_round(session)

        mock_generate.assert_not_called()
        assert round_ is not None
        self.assertEqual(round_.location_id, cached_location.pk)

    def test_a_cached_pick_referencing_a_nonexistent_location_falls_back_to_live_generation(self) -> None:
        profile = _make_profile()
        live_location = _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)

        picked = generate_round_content(SpotGuessrMode.PHOTOS, GameConfig(), [profile], [], None)
        assert picked is not None
        _, content = picked
        # A cache entry naming a location id that doesn't exist - as if the
        # prewarmed location had since been deleted.
        fake_location = Location(pk=live_location.pk + 999_999)
        prewarm.store_for_session(session.pk, 0, fake_location, content)

        round_ = get_or_create_round(session)
        assert round_ is not None
        self.assertEqual(round_.location_id, live_location.pk)


class GetOrCreateRoundConsumesSoloStartPrewarmTests(TestCase):
    def test_round_one_of_a_fresh_solo_session_uses_the_speculative_prewarm(self) -> None:
        profile = _make_profile()
        cached_location = _make_photo_location(profile)
        _make_photo_location(profile)  # a second eligible candidate that must NOT be picked

        config = GameConfig()
        picked = generate_round_content(SpotGuessrMode.PHOTOS, config, [profile], [], None)
        assert picked is not None
        _, content = picked
        prewarm.store_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config, cached_location, content)

        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, config, total_rounds=3)
        # Made the session's only round - see the comment in
        # GetOrCreateRoundConsumesSessionPrewarmTests for why: otherwise
        # creating it also enqueues (and eagerly runs) a prewarm of the next
        # round, which would call the mocked generate_round_content too.
        session.total_rounds = 1
        session.save(update_fields=["total_rounds"])
        with patch("urbanlens.dashboard.services.spotguessr.session.generate_round_content") as mock_generate:
            round_ = get_or_create_round(session)

        mock_generate.assert_not_called()
        assert round_ is not None
        self.assertEqual(round_.location_id, cached_location.pk)

    def test_multiplayer_round_one_never_consumes_a_solo_speculative_prewarm(self) -> None:
        host = _make_profile()
        guest = _make_profile()
        _befriend(host, guest)
        shared_location = _make_location()
        baker.make(Pin, profile=host, location=shared_location)
        baker.make(Pin, profile=guest, location=shared_location)
        baker.make(Image, location=shared_location, media_type=MediaKind.PHOTO, wiki=baker.make(Wiki, location=shared_location))

        config = GameConfig()
        picked = generate_round_content(SpotGuessrMode.PHOTOS, config, [host], [], None)
        assert picked is not None
        _, content = picked
        # Prewarmed as if the host were about to start solo - must not leak
        # into a multiplayer session they end up hosting instead, since
        # multiplayer eligibility (every participant, not just the host) is
        # a different question entirely.
        prewarm.store_for_solo_start(host.pk, SpotGuessrMode.PHOTOS, config, shared_location, content)

        session = start_multiplayer_session(host, SpotGuessrMode.PHOTOS, config, [guest], total_rounds=3)
        join_session(session, guest)
        round_ = begin_session(session, host)

        assert round_ is not None
        # The speculative entry must still be sitting there, unconsumed.
        self.assertIsNotNone(prewarm.consume_for_solo_start(host.pk, SpotGuessrMode.PHOTOS, config))


class PrewarmSpotguessrRoundTaskTests(TestCase):
    def test_prewarms_the_given_round_index(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)

        result = prewarm_spotguessr_round(session.pk, 0)

        self.assertTrue(result)
        cached = prewarm.consume_for_session(session.pk, 0)
        assert cached is not None
        self.assertEqual(cached[0], location.pk)

    def test_a_round_that_already_exists_is_a_no_op(self) -> None:
        profile = _make_profile()
        _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)
        round_ = get_or_create_round(session)
        assert round_ is not None

        result = prewarm_spotguessr_round(session.pk, 0)

        self.assertFalse(result)
        self.assertIsNone(prewarm.consume_for_session(session.pk, 0))

    def test_an_ended_session_is_a_no_op(self) -> None:
        profile = _make_profile()
        _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)
        session.status = GameSessionStatus.COMPLETED
        session.save(update_fields=["status"])

        result = prewarm_spotguessr_round(session.pk, 0)

        self.assertFalse(result)

    def test_creating_a_round_live_triggers_the_background_prewarm_of_the_next_one(self) -> None:
        """End-to-end: get_or_create_round's own enqueue (run inline under
        Celery's eager-mode test setting) should leave the *next* round
        already cached, so completing the current one serves the next from
        cache instead of generating it live."""
        profile = _make_profile()
        _make_photo_location(profile)
        _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)
        # 2 rounds total (below the MIN_ROUNDS_PER_SESSION=3 floor
        # start_solo_session's own total_rounds param would clamp to), so
        # creating round 2 doesn't *also* enqueue - and eagerly run - a
        # prewarm for a round after it, which would call the mocked
        # generate_round_content too and confuse the assertion below.
        session.total_rounds = 2
        session.save(update_fields=["total_rounds"])

        round_1 = get_or_create_round(session)
        assert round_1 is not None

        guess_point = Point(float(round_1.location.longitude), float(round_1.location.latitude), srid=4326)
        with patch("urbanlens.dashboard.services.spotguessr.session.generate_round_content") as mock_generate:
            submit_guess(round_1, profile, guess_point)

        mock_generate.assert_not_called()
        round_2 = GameRound.objects.for_session(session).get(sequence_index=1)
        self.assertNotEqual(round_2.location_id, round_1.location_id)

    def test_the_final_rounds_own_creation_does_not_enqueue_a_further_prewarm(self) -> None:
        profile = _make_profile()
        _make_photo_location(profile)
        session = start_solo_session(profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=1)

        with patch("urbanlens.dashboard.tasks.prewarm_spotguessr_round") as mock_task:
            round_ = get_or_create_round(session)

        assert round_ is not None
        mock_task.assert_not_called()


class PrewarmSpotguessrSoloStartTaskTests(TestCase):
    def test_stores_a_pick_the_live_start_flow_can_later_consume(self) -> None:
        profile = _make_profile()
        location = _make_photo_location(profile)
        config = GameConfig(difficulty=0.3)

        result = prewarm_spotguessr_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config.to_dict())

        self.assertTrue(result)
        cached = prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config)
        assert cached is not None
        self.assertEqual(cached[0], location.pk)

    def test_no_eligible_locations_returns_false_without_caching_anything(self) -> None:
        profile = _make_profile()
        config = GameConfig()

        result = prewarm_spotguessr_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config.to_dict())

        self.assertFalse(result)
        self.assertIsNone(prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, config))

    def test_unknown_config_keys_are_ignored_rather_than_raising(self) -> None:
        profile = _make_profile()
        _make_photo_location(profile)

        result = prewarm_spotguessr_solo_start(profile.pk, SpotGuessrMode.PHOTOS, {"difficulty": 0.5, "not_a_real_field": "x"})

        self.assertTrue(result)
        prewarm.consume_for_solo_start(profile.pk, SpotGuessrMode.PHOTOS, GameConfig(difficulty=0.5))
