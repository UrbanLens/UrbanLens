"""HTTP-level tests for controllers.spotguessr - auth boundaries, request validation, and answer-leak safety."""

from __future__ import annotations

from itertools import count
import json

from django.core.files.base import ContentFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameRound, GameSession, GameSessionStatus, SpotGuessrMode, SpotGuessrPreference
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr.session import GameConfig, begin_session, join_session, start_multiplayer_session

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class SpotGuessrStartViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(
            Image,
            location=self.location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            image=ContentFile(b"fake image bytes", name="test.jpg"),
            wiki=baker.make(Wiki, location=self.location),
        )
        self.client.force_login(self.profile.user)
        self.start_url = reverse("spotguessr.start")

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 302)

    def test_starting_a_session_returns_a_round_without_leaking_the_answer(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "3"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["finished"])
        self.assertIn("round_id", data["round"])
        self.assertIn("image_url", data["round"])
        self.assertNotIn("latitude", json.dumps(data))
        self.assertEqual(GameSession.objects.get(pk=data["session_id"]).total_rounds, 3)

    def test_allow_arbitrary_external_photos_is_persisted_on_the_session_config(self) -> None:
        response = self.client.post(self.start_url, {"allow_arbitrary_external_photos": "on"})
        self.assertEqual(response.status_code, 200)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertTrue(session.config["allow_arbitrary_external_photos"])

    def test_allow_arbitrary_external_photos_defaults_off(self) -> None:
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertFalse(session.config["allow_arbitrary_external_photos"])

    def test_invalid_difficulty_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"difficulty": "not-a-number"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_geo_bounds_json_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"geo_bounds": "{not valid json"})
        self.assertEqual(response.status_code, 400)

    def test_geo_bounds_that_isnt_a_geometry_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"geo_bounds": json.dumps({"foo": "bar"})})
        self.assertEqual(response.status_code, 400)

    def test_no_eligible_locations_reports_error_code_without_creating_a_session(self) -> None:
        """A profile with no pins used to get a fake 'finished' summary (0 rounds
        played, GameSession created and immediately COMPLETED) indistinguishable
        from a real completed game - see the SpotGuessr UX
        rewrite notes. It must now get a distinct error_code and no session at all."""
        other_profile = _make_profile()
        self.client.force_login(other_profile.user)
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("error_code"), "no_eligible_locations")
        self.assertNotIn("finished", data)
        self.assertNotIn("summary", data)
        self.assertFalse(GameSession.objects.filter(host_profile=other_profile).exists())

    def test_a_pinned_location_with_no_usable_photo_reports_no_eligible_locations_too(self) -> None:
        """Regression guard: a profile can have pins (passing the cheap
        pre-check) but every pinned location's photo pool turns out
        unusable once round generation actually runs (e.g. filtered out by
        relevance) - this used to silently complete the freshly-created
        session and report a fake 'Game over! Your score: 0 pts', which
        reads as a real (if confusing) finished game rather than nothing
        having been playable at all."""
        bare_profile = _make_profile()
        bare_location = _make_location()
        baker.make(Pin, profile=bare_profile, location=bare_location)
        # No Image at all for bare_location - get_or_create_round can never
        # produce a Photos-mode round for it.
        self.client.force_login(bare_profile.user)
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("error_code"), "no_eligible_locations")
        self.assertNotIn("finished", data)
        self.assertNotIn("summary", data)

    def test_no_eligible_locations_within_a_restrictive_geo_bounds_is_also_reported(self) -> None:
        far_away_bounds = json.dumps(
            {"type": "Polygon", "coordinates": [[[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]]]},
        )
        response = self.client.post(self.start_url, {"geo_bounds": far_away_bounds})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("error_code"), "no_eligible_locations")

    def test_a_valid_round_time_limit_is_persisted_on_the_session_config(self) -> None:
        response = self.client.post(self.start_url, {"round_time_limit_seconds": "60"})
        self.assertEqual(response.status_code, 200)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.config["round_time_limit_seconds"], 60)

    def test_no_round_time_limit_defaults_to_untimed(self) -> None:
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertIsNone(session.config["round_time_limit_seconds"])

    def test_an_unsupported_round_time_limit_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"round_time_limit_seconds": "45"})
        self.assertEqual(response.status_code, 400)

    def test_a_non_numeric_round_time_limit_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"round_time_limit_seconds": "soon"})
        self.assertEqual(response.status_code, 400)

    def test_a_valid_label_id_is_persisted_on_the_session_config(self) -> None:
        label = baker.make(Label, name="Factories", profile=self.profile)
        Pin.objects.get(profile=self.profile, location=self.location).labels.add(label)
        response = self.client.post(self.start_url, {"label_id": str(label.pk)})
        self.assertEqual(response.status_code, 200, response.json())
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.config["label_id"], label.pk)

    def test_no_label_id_defaults_to_no_label_filter(self) -> None:
        response = self.client.post(self.start_url, {})
        self.assertEqual(response.status_code, 200)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertIsNone(session.config["label_id"])

    def test_a_non_numeric_label_id_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"label_id": "not-an-id"})
        self.assertEqual(response.status_code, 400)

    def test_a_nonexistent_label_id_is_rejected(self) -> None:
        response = self.client.post(self.start_url, {"label_id": "999999"})
        self.assertEqual(response.status_code, 400)

    def test_another_profiles_private_label_id_is_rejected(self) -> None:
        """Guards against using this endpoint to probe for another profile's private labels."""
        other_profile = _make_profile()
        someone_elses_label = baker.make(Label, name="Someone else's spots", profile=other_profile)
        response = self.client.post(self.start_url, {"label_id": str(someone_elses_label.pk)})
        self.assertEqual(response.status_code, 400)

    def test_a_global_label_id_is_accepted(self) -> None:
        global_label = baker.make(Label, name="Abandoned", profile=None)
        response = self.client.post(self.start_url, {"label_id": str(global_label.pk)})
        self.assertEqual(response.status_code, 200, response.json())

    def test_label_filter_narrows_which_location_the_round_uses(self) -> None:
        label = baker.make(Label, name="Factories", profile=self.profile)
        other_location = _make_location()
        other_pin = baker.make(Pin, profile=self.profile, location=other_location)
        other_pin.labels.add(label)
        baker.make(
            Image,
            location=other_location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            image=ContentFile(b"fake image bytes", name="other.jpg"),
            wiki=baker.make(Wiki, location=other_location),
        )
        # self.location (from setUp) is pinned but never labeled, so only
        # other_location should ever be picked once label_id narrows the pool.

        for _ in range(5):
            response = self.client.post(self.start_url, {"label_id": str(label.pk), "total_rounds": "1"})
            self.assertEqual(response.status_code, 200)
            round_ = GameRound.objects.get(pk=response.json()["round"]["round_id"])
            self.assertEqual(round_.location_id, other_location.pk)

    def test_round_payload_reports_whether_it_shows_imagery(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "1"})
        self.assertTrue(response.json()["round"]["shows_imagery"])

    def test_round_payload_has_no_expiry_when_untimed(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "1"})
        self.assertIsNone(response.json()["round"].get("expires_at"))

    def test_round_payload_reports_an_expiry_when_timed(self) -> None:
        response = self.client.post(self.start_url, {"total_rounds": "1", "round_time_limit_seconds": "30"})
        self.assertIsNotNone(response.json()["round"]["expires_at"])


class SpotGuessrGuessFlowTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))
        self.client.force_login(self.profile.user)
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1"}).json()
        self.session_id = start["session_id"]
        self.round_id = start["round"]["round_id"]
        self.guess_url = reverse("spotguessr.guess", args=[self.session_id, self.round_id])
        self.summary_url = reverse("spotguessr.summary", args=[self.session_id])

    def test_guessing_reveals_the_location_and_score(self) -> None:
        response = self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["points"], 5000)
        self.assertAlmostEqual(data["actual_latitude"], float(self.location.latitude), places=4)

    def test_a_second_guess_on_the_same_round_is_rejected(self) -> None:
        payload = {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)}
        self.client.post(self.guess_url, payload)
        response = self.client.post(self.guess_url, payload)
        self.assertEqual(response.status_code, 400)

    def test_missing_coordinates_are_rejected(self) -> None:
        response = self.client.post(self.guess_url, {})
        self.assertEqual(response.status_code, 400)

    def test_a_non_participant_cannot_guess_on_someone_elses_session(self) -> None:
        outsider = _make_profile()
        self.client.force_login(outsider.user)
        response = self.client.post(self.guess_url, {"latitude": "0", "longitude": "0"})
        self.assertEqual(response.status_code, 404)

    def test_summary_reports_the_final_score(self) -> None:
        self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        response = self.client.get(self.summary_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["participants"][0]["total_points"], 5000)


class SpotGuessrPinsViewTests(TestCase):
    def test_only_returns_the_requesting_profiles_own_pins(self) -> None:
        mine = _make_profile()
        theirs = _make_profile()
        baker.make(Pin, profile=mine, location=_make_location())
        baker.make(Pin, profile=theirs, location=_make_location())

        self.client.force_login(mine.user)
        response = self.client.get(reverse("spotguessr.pins"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["pins"]), 1)


class SpotGuessrPhotoFeedbackViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))
        self.client.force_login(self.profile.user)
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1"}).json()
        self.session_id = start["session_id"]
        self.round_id = start["round"]["round_id"]
        self.feedback_url = reverse("spotguessr.photo_feedback", args=[self.session_id, self.round_id])

    def test_reacting_before_guessing_is_rejected(self) -> None:
        response = self.client.post(self.feedback_url, {"kind": "thumbs_up"})
        self.assertEqual(response.status_code, 403)

    def test_thumbs_up_after_guessing_is_recorded(self) -> None:
        from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind

        self.client.post(reverse("spotguessr.guess", args=[self.session_id, self.round_id]), {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)})
        response = self.client.post(self.feedback_url, {"kind": "thumbs_up"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kind"], "thumbs_up")
        feedback = GamePhotoFeedback.objects.get(round_id=self.round_id, profile=self.profile)
        self.assertEqual(feedback.kind, GamePhotoFeedbackKind.THUMBS_UP)

    def test_explicit_reaction_overrides_the_automatic_no_reaction_backfill(self) -> None:
        """Solo play completes (and backfills NO_REACTION for) the round the
        instant its one guess is submitted - an explicit reaction afterward
        must still win."""
        from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind

        self.client.post(reverse("spotguessr.guess", args=[self.session_id, self.round_id]), {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)})
        self.assertEqual(GamePhotoFeedback.objects.get(round_id=self.round_id, profile=self.profile).kind, GamePhotoFeedbackKind.NO_REACTION)

        self.client.post(self.feedback_url, {"kind": "reported"})
        self.assertEqual(GamePhotoFeedback.objects.get(round_id=self.round_id, profile=self.profile).kind, GamePhotoFeedbackKind.REPORTED)

    def test_an_invalid_kind_is_rejected(self) -> None:
        self.client.post(reverse("spotguessr.guess", args=[self.session_id, self.round_id]), {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)})
        response = self.client.post(self.feedback_url, {"kind": "not_a_real_kind"})
        self.assertEqual(response.status_code, 400)

    def test_a_non_participant_cannot_react_on_someone_elses_session(self) -> None:
        outsider = _make_profile()
        self.client.force_login(outsider.user)
        response = self.client.post(self.feedback_url, {"kind": "thumbs_up"})
        self.assertEqual(response.status_code, 404)

    def test_a_named_place_round_has_no_photo_to_react_to(self) -> None:
        named_location = _make_location()
        baker.make(Pin, profile=self.profile, location=named_location)
        baker.make(Wiki, location=named_location, name="Old Mill House")
        response = self.client.post(reverse("spotguessr.start"), {"mode": "named_place", "total_rounds": "1"})
        data = response.json()
        guess_url = reverse("spotguessr.guess", args=[data["session_id"], data["round"]["round_id"]])
        self.client.post(guess_url, {"latitude": str(named_location.latitude), "longitude": str(named_location.longitude)})

        feedback_url = reverse("spotguessr.photo_feedback", args=[data["session_id"], data["round"]["round_id"]])
        response = self.client.post(feedback_url, {"kind": "thumbs_up"})
        self.assertEqual(response.status_code, 400)


class SpotGuessrSettingsViewTests(TestCase):
    def test_toggling_show_ratings_to_friends(self) -> None:
        profile = _make_profile()
        self.client.force_login(profile.user)

        response = self.client.post(reverse("spotguessr.settings"), {"show_ratings_to_friends": "off"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SpotGuessrPreference.objects.get(profile=profile).show_ratings_to_friends)


class SpotGuessrMultiplayerGuessRevealTests(TestCase):
    """The answer must stay hidden from an early guesser until every joined participant has guessed.

    Per "Real-time sync" in docs/designs/drafts/spotguessr.md: guess.submitted carries
    no coordinates or score, and the answer only goes out with round.revealed.
    Without this, the first guesser could read it off their own HTTP response
    and relay it to teammates over session chat before they'd guessed too.
    """

    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        friendship = Friendship.request(self.host, self.guest)
        assert friendship is not None
        friendship.accept()

        self.location = _make_location()
        baker.make(Pin, profile=self.host, location=self.location)
        baker.make(Pin, profile=self.guest, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))

        session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])
        join_session(session, self.guest)
        round_ = begin_session(session, self.host)
        assert round_ is not None
        self.guess_url = reverse("spotguessr.guess", args=[session.pk, round_.pk])

    def test_the_first_guesser_does_not_receive_the_answer_yet(self) -> None:
        self.client.force_login(self.host.user)
        response = self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["revealed"])
        self.assertNotIn("actual_latitude", data)
        self.assertNotIn("actual_longitude", data)
        self.assertNotIn("location_name", data)
        self.assertEqual(data["points"], 5000)  # the guesser's own score is still reported immediately

    def test_the_last_guesser_receives_the_answer(self) -> None:
        self.client.force_login(self.host.user)
        self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        self.client.force_login(self.guest.user)
        response = self.client.post(
            self.guess_url,
            {"latitude": str(self.location.latitude), "longitude": str(self.location.longitude)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["revealed"])
        self.assertAlmostEqual(data["actual_latitude"], float(self.location.latitude), places=4)


class SpotGuessrNoEligibleLocationsMidGameTests(TestCase):
    """Unlike solo play, a multiplayer lobby can't be eligibility-checked at
    start - the invitees haven't joined (and so haven't contributed their
    pins to the eligible set) yet. The host only discovers there's nothing
    to play once they begin the game. That must be reported distinctly from
    a real finish, and must not mark the session COMPLETED - see
    SpotGuessrBeginView's docstring."""

    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        friendship = Friendship.request(self.host, self.guest)
        assert friendship is not None
        friendship.accept()
        # The host's pin isn't shared by the guest, so once both are JOINED
        # there is nothing eligible for the pair.
        baker.make(Pin, profile=self.host, location=_make_location())

        self.client.force_login(self.host.user)
        start = self.client.post(reverse("spotguessr.start"), {"invite_profile_ids": [str(self.guest.pk)]})
        self.session_id = start.json()["session_id"]

        self.client.force_login(self.guest.user)
        self.client.post(reverse("spotguessr.join", args=[self.session_id]))

    def test_begin_reports_no_eligible_locations_without_completing_the_session(self) -> None:
        self.client.force_login(self.host.user)
        response = self.client.post(reverse("spotguessr.begin", args=[self.session_id]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["finished"])
        self.assertTrue(data.get("no_eligible_locations"))
        self.assertNotIn("summary", data)

        session = GameSession.objects.get(pk=self.session_id)
        self.assertEqual(session.status, GameSessionStatus.ACTIVE)
        self.assertEqual(session.participants.get(profile=self.host).total_points, 0)


class SpotGuessrEndSessionViewTests(TestCase):
    def setUp(self) -> None:
        self.host = _make_profile()
        self.guest = _make_profile()
        friendship = Friendship.request(self.host, self.guest)
        assert friendship is not None
        friendship.accept()
        self.location = _make_location()
        baker.make(Pin, profile=self.host, location=self.location)
        baker.make(Pin, profile=self.guest, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))

        self.session = start_multiplayer_session(self.host, SpotGuessrMode.PHOTOS, GameConfig(), [self.guest])
        join_session(self.session, self.guest)
        begin_session(self.session, self.host)
        self.end_url = reverse("spotguessr.end", args=[self.session.pk])

    def test_the_host_can_end_the_game_early(self) -> None:
        self.client.force_login(self.host.user)
        response = self.client.post(self.end_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["finished"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, GameSessionStatus.COMPLETED)

    def test_a_non_host_participant_cannot_end_the_game(self) -> None:
        self.client.force_login(self.guest.user)
        response = self.client.post(self.end_url)
        self.assertEqual(response.status_code, 400)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, GameSessionStatus.ACTIVE)

    def test_a_non_participant_gets_a_404(self) -> None:
        outsider = _make_profile()
        self.client.force_login(outsider.user)
        response = self.client.post(self.end_url)
        self.assertEqual(response.status_code, 404)


class SpotGuessrRoundTimeoutViewTests(TestCase):
    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))
        self.client.force_login(self.profile.user)

    def test_a_call_before_the_timer_expires_is_a_no_op(self) -> None:
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1", "round_time_limit_seconds": "120"}).json()
        timeout_url = reverse("spotguessr.round_timeout", args=[start["session_id"], start["round"]["round_id"]])

        response = self.client.post(timeout_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["revealed"])
        round_ = GameRound.objects.get(pk=start["round"]["round_id"])
        self.assertIsNone(round_.revealed_at)

    def test_an_untimed_session_never_times_out(self) -> None:
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1"}).json()
        timeout_url = reverse("spotguessr.round_timeout", args=[start["session_id"], start["round"]["round_id"]])

        response = self.client.post(timeout_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["revealed"])

    def test_a_call_after_the_timer_expires_reveals_the_round(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1", "round_time_limit_seconds": "30"}).json()
        round_id = start["round"]["round_id"]
        GameRound.objects.filter(pk=round_id).update(created=timezone.now() - timedelta(seconds=45))
        timeout_url = reverse("spotguessr.round_timeout", args=[start["session_id"], round_id])

        response = self.client.post(timeout_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["revealed"])
        round_ = GameRound.objects.get(pk=round_id)
        self.assertIsNotNone(round_.revealed_at)

    def test_a_non_participant_gets_a_404(self) -> None:
        start = self.client.post(reverse("spotguessr.start"), {"total_rounds": "1", "round_time_limit_seconds": "30"}).json()
        timeout_url = reverse("spotguessr.round_timeout", args=[start["session_id"], start["round"]["round_id"]])

        outsider = _make_profile()
        self.client.force_login(outsider.user)
        response = self.client.post(timeout_url)
        self.assertEqual(response.status_code, 404)


class SpotGuessrHomeViewPrewarmTests(TestCase):
    """Visiting the overview page should speculatively prewarm a solo player's likely round 1 - see services.spotguessr.prewarm."""

    def setUp(self) -> None:
        self.profile = _make_profile()
        self.location = _make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        baker.make(Image, location=self.location, media_type=MediaKind.PHOTO, latitude=None, longitude=None, wiki=baker.make(Wiki, location=self.location))
        self.client.force_login(self.profile.user)

    def test_visiting_the_page_prewarms_round_one_for_the_default_mode(self) -> None:
        from urbanlens.dashboard.services.spotguessr import prewarm
        from urbanlens.dashboard.tasks import prewarm_spotguessr_solo_start

        # The view enqueues the speculative prewarm rather than doing it; with
        # no worker draining the broker it would simply never happen (see
        # core.tests.celery_inline).
        with tasks_run_inline(prewarm_spotguessr_solo_start):
            response = self.client.get(reverse("spotguessr"))

        self.assertEqual(response.status_code, 200)
        cached = prewarm.consume_for_solo_start(self.profile.pk, SpotGuessrMode.PHOTOS, GameConfig())
        assert cached is not None
        self.assertEqual(cached[0], self.location.pk)

    def test_resuming_a_specific_session_does_not_trigger_the_speculative_prewarm(self) -> None:
        from urbanlens.dashboard.services.spotguessr import prewarm
        from urbanlens.dashboard.services.spotguessr.session import start_solo_session

        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), total_rounds=3)

        response = self.client.get(reverse("spotguessr"), {"session": session.pk})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(prewarm.consume_for_solo_start(self.profile.pk, SpotGuessrMode.PHOTOS, GameConfig()))
