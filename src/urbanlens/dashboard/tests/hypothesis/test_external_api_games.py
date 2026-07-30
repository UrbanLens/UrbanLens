"""Tests for the external API's SpotGuessr surface.

SpotGuessr is the first *game* this API exposes, and games have a failure mode
the CRUD domains do not: a wrong answer is silently playable. So alongside the
usual scope and enumeration checks, these tests pin down the four things that
would each quietly ruin the game rather than error:

1. the pre-guess payload must never carry anything naming the answer - not in
   the JSON, and not in the image bytes either;
2. a session this API cannot finish (multiplayer, or a lobby) must be refused
   loudly, because the alternative is a client polling forever for a reveal
   that only ever arrives over a WebSocket it isn't on;
3. ``Point`` takes longitude first, and getting it backwards produces a
   perfectly valid point somewhere else on Earth rather than an error;
4. the current-round GET creates rounds, so it must be throttled as a write.
"""

from __future__ import annotations

from datetime import timedelta
import io
from itertools import count
import json

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage
from PIL.TiffImagePlugin import IFDRational

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.throttling import TIER_WRITE
from urbanlens.dashboard.external_api.views_games import SpotGuessrRoundView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind, GameRound, GameSession, GameSessionStatus, Guess, SpotGuessrMode
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.spotguessr.session import GameConfig, start_multiplayer_session, start_solo_session

_coordinate_counter = count()

_GAME_SCOPES = [ApiKeyScope.GAMES_READ.value, ApiKeyScope.GAMES_WRITE.value]

#: Caption of exactly the kind EXIF hands us: it names the place outright.
_ANSWER_NAMING_CAPTION = "Old Mill House, Troy NY"


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _gps_jpeg_bytes() -> bytes:
    """A JPEG carrying GPS coordinates and a place-naming description in its EXIF.

    This is what a phone actually stores, and every one of these tags points at
    the round's answer - which is why the round-image endpoint re-encodes rather
    than deleting known-bad tags one at a time.
    """
    img = PILImage.new("RGB", (16, 16), color=(120, 60, 30))
    exif = PILImage.Exif()
    exif[0x010E] = _ANSWER_NAMING_CAPTION  # ImageDescription
    gps_ifd = exif.get_ifd(0x8825)  # 34853 - GPSInfo IFD
    gps_ifd[1] = "N"
    gps_ifd[2] = (IFDRational(42, 1), IFDRational(39, 1), IFDRational(0, 1))
    gps_ifd[3] = "W"
    gps_ifd[4] = (IFDRational(73, 1), IFDRational(45, 1), IFDRational(0, 1))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


class _SpotGuessrApiTestCase(TestCase):
    """Shared fixture: a key owner with one playable pinned location, plus a bystander."""

    def setUp(self) -> None:
        """Create the key owner, something for them to play, and a games-scoped key."""
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="player")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other_profile = Profile.objects.get(user=self.other_user)

        self.location = self._make_location()
        baker.make(Pin, profile=self.profile, location=self.location)
        self.image = baker.make(
            Image,
            location=self.location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            caption=_ANSWER_NAMING_CAPTION,
            image=ContentFile(b"fake image bytes", name="round.jpg"),
            wiki=baker.make(Wiki, location=self.location),
        )

        self.raw_key = self._key_with_scopes([*_GAME_SCOPES, ApiKeyScope.PROFILE_READ.value])

    def _make_location(self) -> Location:
        """A location with coordinates distinct from every other one this module makes."""
        offset = next(_coordinate_counter)
        return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}", official_name="Old Mill House")

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _get(self, name: str, *args, key: str | None = None, data: dict | None = None):
        """GET a named external-API route with a bearer key."""
        return self.client.get(reverse(name, args=args), data, **_bearer(key or self.raw_key))

    def _post(self, name: str, *args, body: dict | None = None, key: str | None = None):
        """POST JSON to a named external-API route with a bearer key."""
        return self.client.post(reverse(name, args=args), body or {}, content_type="application/json", **_bearer(key or self.raw_key))

    def _patch(self, name: str, *args, body: dict | None = None, key: str | None = None):
        """PATCH JSON to a named external-API route with a bearer key."""
        return self.client.patch(reverse(name, args=args), body or {}, content_type="application/json", **_bearer(key or self.raw_key))

    def _start_session(self) -> dict:
        """Start a solo session over the API and return the response body."""
        response = self._post("external_api:games.spotguessr.sessions", body={"total_rounds": 3})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class SpotGuessrOverviewTests(_SpotGuessrApiTestCase):
    """The start screen's data, and the separate gate on other people's ratings."""

    def test_overview_returns_the_limits_and_the_players_own_state(self) -> None:
        response = self._get("external_api:games.spotguessr")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual({mode["value"] for mode in body["modes"]}, set(SpotGuessrMode.values))
        self.assertEqual(body["min_rounds"], 3)
        self.assertEqual(body["max_rounds"], 20)
        self.assertIn("round_time_limit_choices", body)
        self.assertIn("last_config", body)
        self.assertTrue(body["show_ratings_to_friends"])
        self.assertIsNone(body["own_rating"])
        self.assertIsNone(body["active_session_id"])

    def test_active_session_id_points_at_a_resumable_solo_game(self) -> None:
        """A backgrounded client comes back and needs to know what it was playing."""
        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        body = self._get("external_api:games.spotguessr").json()
        self.assertEqual(body["active_session_id"], session.pk)

    def test_a_multiplayer_game_is_never_offered_as_resumable(self) -> None:
        """Resuming into one would leave the client polling for a reveal it can't receive."""
        friendship = Friendship.request(self.profile, self.other_profile)
        assert friendship is not None
        friendship.accept()
        session = start_multiplayer_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), [self.other_profile])
        GameSession.objects.filter(pk=session.pk).update(status=GameSessionStatus.ACTIVE)
        body = self._get("external_api:games.spotguessr").json()
        self.assertIsNone(body["active_session_id"])

    def test_friend_ratings_need_the_social_scope(self) -> None:
        """Naming the user's friends is a social read, not a game read."""
        body = self._get("external_api:games.spotguessr").json()
        self.assertNotIn("friend_ratings", body)

        social_key = self._key_with_scopes([*_GAME_SCOPES, ApiKeyScope.SOCIAL_READ.value])
        body = self._get("external_api:games.spotguessr", key=social_key).json()
        self.assertIn("friend_ratings", body)

    def test_a_credential_without_the_games_scope_is_refused(self) -> None:
        pins_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        response = self._get("external_api:games.spotguessr", key=pins_key)
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.json())


class SpotGuessrSessionCreateTests(_SpotGuessrApiTestCase):
    """Starting a solo session, and what happens when there is nothing to play."""

    def test_start_returns_201_with_a_first_round(self) -> None:
        body = self._start_session()
        self.assertFalse(body["finished"])
        self.assertEqual(body["status"], GameSessionStatus.ACTIVE)
        self.assertEqual(body["total_rounds"], 3)
        self.assertIn("round_id", body["round"])
        self.assertIn("image_url", body["round"])

    def test_the_first_round_never_names_the_answer(self) -> None:
        """The whole game rests on this: no caption, no coordinates, no place name."""
        body = self._start_session()
        serialized = json.dumps(body)
        self.assertNotIn("image_caption", body["round"])
        self.assertNotIn(_ANSWER_NAMING_CAPTION, serialized)
        self.assertNotIn("Old Mill House", serialized)
        self.assertNotIn("actual_latitude", serialized)

    def test_the_round_payload_is_a_whitelist(self) -> None:
        """A field added to a mode strategy must not become part of the question by default."""
        from urbanlens.dashboard.external_api.serializers_games import ROUND_PAYLOAD_FIELDS

        body = self._start_session()
        self.assertTrue(set(body["round"]).issubset(set(ROUND_PAYLOAD_FIELDS)))

    def test_total_rounds_is_clamped_rather_than_rejected(self) -> None:
        response = self._post("external_api:games.spotguessr.sessions", body={"total_rounds": 1})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["total_rounds"], 3)

    def test_geo_bounds_is_a_geojson_object_not_a_string(self) -> None:
        """A JSON client already has an object; making it re-encode invites double encoding."""
        polygon = {"type": "Polygon", "coordinates": [[[-74.0, 42.0], [-74.0, 43.0], [-73.0, 43.0], [-73.0, 42.0], [-74.0, 42.0]]]}
        response = self._post("external_api:games.spotguessr.sessions", body={"geo_bounds": polygon})
        self.assertEqual(response.status_code, 201, response.content)
        session = GameSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.config["geo_bounds_geojson"], polygon)

    def test_a_malformed_geo_bounds_is_a_400_not_a_500(self) -> None:
        response = self._post("external_api:games.spotguessr.sessions", body={"geo_bounds": {"nonsense": True}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("fields", response.json())

    def test_nothing_eligible_is_a_409_and_creates_no_session(self) -> None:
        """The internal endpoint answers 200 for this; a 200 meaning "did not happen" is a wart."""
        empty_user = baker.make(User, username="nothing-pinned")
        key = self._key_with_scopes(_GAME_SCOPES, user=empty_user)
        response = self._post("external_api:games.spotguessr.sessions", key=key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "no_eligible_locations")
        self.assertFalse(GameSession.objects.filter(host_profile__user=empty_user).exists())

    def test_starting_needs_the_write_scope(self) -> None:
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._post("external_api:games.spotguessr.sessions", key=read_key)
        self.assertEqual(response.status_code, 403)

    def test_the_start_config_becomes_the_players_remembered_defaults(self) -> None:
        self._post("external_api:games.spotguessr.sessions", body={"difficulty": 0.8, "use_aliases": False})
        body = self._get("external_api:games.spotguessr").json()
        self.assertEqual(body["last_config"]["difficulty"], 0.8)
        self.assertFalse(body["last_config"]["use_aliases"])


class SpotGuessrRoundTests(_SpotGuessrApiTestCase):
    """Fetching the current round - the call that quietly creates them."""

    def test_the_current_round_is_returned_for_a_reload(self) -> None:
        started = self._start_session()
        response = self._get("external_api:games.spotguessr.sessions.round", started["session_id"])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["finished"])
        self.assertEqual(body["round"]["round_id"], started["round"]["round_id"])

    def test_the_round_get_is_throttled_as_a_write(self) -> None:
        """It creates rounds and persists bonus_scope, so the loose read budget is wrong for it."""
        self.assertEqual(SpotGuessrRoundView.throttle_tier_by_method["GET"], TIER_WRITE)

    def test_another_users_session_is_a_404(self) -> None:
        theirs = start_solo_session(self.other_profile, SpotGuessrMode.PHOTOS, GameConfig())
        response = self._get("external_api:games.spotguessr.sessions.round", theirs.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_session_that_never_existed_looks_identical(self) -> None:
        """Sequential ids make a 403 an enumeration oracle, so both cases are 404."""
        response = self._get("external_api:games.spotguessr.sessions.round", 987654321)
        self.assertEqual(response.status_code, 404)


class SpotGuessrGuessTests(_SpotGuessrApiTestCase):
    """Submitting a guess."""

    def setUp(self) -> None:
        """Start a session so every test here has a live round to guess on."""
        super().setUp()
        started = self._start_session()
        self.session_id = started["session_id"]
        self.round_id = started["round"]["round_id"]

    def test_guessing_the_exact_coordinates_scores_a_zero_distance(self) -> None:
        """Proves the Point argument order: swapping lat/lng yields a valid point ~5000km away.

        Nothing raises on a swap - the guess simply scores as a total miss - so
        this assertion is the only thing standing between the game and a
        silently unwinnable one.
        """
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            self.round_id,
            body={"latitude": float(self.location.latitude), "longitude": float(self.location.longitude)},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertLess(body["distance_meters"], 1.0)
        self.assertGreater(body["points"], 0)

    def test_the_answer_arrives_with_the_reveal(self) -> None:
        """Solo play reveals on the guess itself, so the answer is due immediately."""
        body = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            self.round_id,
            body={"latitude": 42.0, "longitude": -73.0},
        ).json()
        self.assertTrue(body["revealed"])
        self.assertAlmostEqual(body["actual_latitude"], float(self.location.latitude), places=3)
        self.assertAlmostEqual(body["actual_longitude"], float(self.location.longitude), places=3)
        self.assertEqual(body["location_name"], "Old Mill House")

    def test_a_second_guess_on_the_same_round_is_a_400(self) -> None:
        payload = {"latitude": 42.0, "longitude": -73.0}
        self._post("external_api:games.spotguessr.sessions.rounds.guess", self.session_id, self.round_id, body=payload)
        response = self._post("external_api:games.spotguessr.sessions.rounds.guess", self.session_id, self.round_id, body=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Guess.objects.filter(round_id=self.round_id, profile=self.profile).count(), 1)

    def test_an_unknown_round_is_a_404(self) -> None:
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            987654321,
            body={"latitude": 42.0, "longitude": -73.0},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_round_belonging_to_another_session_is_a_404(self) -> None:
        """The round id is scoped to the session in the URL, not looked up globally."""
        other_session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        other_round = GameRound.objects.create(session=other_session, sequence_index=0, location=self.location)
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            other_round.pk,
            body={"latitude": 42.0, "longitude": -73.0},
        )
        self.assertEqual(response.status_code, 404)

    def test_an_out_of_range_latitude_is_rejected(self) -> None:
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            self.round_id,
            body={"latitude": 200.0, "longitude": -73.0},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("fields", response.json())

    def test_guessing_needs_the_write_scope(self) -> None:
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            self.round_id,
            body={"latitude": 42.0, "longitude": -73.0},
            key=read_key,
        )
        self.assertEqual(response.status_code, 403)


class SpotGuessrSessionListTests(_SpotGuessrApiTestCase):
    """The history/resume list, which has no internal equivalent."""

    def test_the_list_is_paginated_and_flags_multiplayer(self) -> None:
        solo = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        friendship = Friendship.request(self.profile, self.other_profile)
        assert friendship is not None
        friendship.accept()
        multi = start_multiplayer_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), [self.other_profile])

        response = self._get("external_api:games.spotguessr.sessions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        by_id = {row["session_id"]: row for row in body["results"]}
        self.assertFalse(by_id[solo.pk]["is_multiplayer"])
        self.assertTrue(by_id[multi.pk]["is_multiplayer"])

    def test_profiles_are_identified_by_slug_not_primary_key(self) -> None:
        start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        row = self._get("external_api:games.spotguessr.sessions").json()["results"][0]
        self.assertEqual(row["host_profile_slug"], self.profile.slug)
        self.assertNotIn("host_profile_id", row)

    def test_the_list_can_be_filtered_by_status(self) -> None:
        start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        response = self._get("external_api:games.spotguessr.sessions", data={"status": GameSessionStatus.COMPLETED})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_another_users_sessions_are_never_listed(self) -> None:
        start_solo_session(self.other_profile, SpotGuessrMode.PHOTOS, GameConfig())
        self.assertEqual(self._get("external_api:games.spotguessr.sessions").json()["count"], 0)

    def test_the_detail_route_returns_one_session(self) -> None:
        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        response = self._get("external_api:games.spotguessr.sessions.detail", session.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], session.pk)


class SpotGuessrSummaryTests(_SpotGuessrApiTestCase):
    """The final scoreboard."""

    def test_the_summary_identifies_participants_by_slug(self) -> None:
        started = self._start_session()
        self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            started["session_id"],
            started["round"]["round_id"],
            body={"latitude": 42.0, "longitude": -73.0},
        )
        response = self._get("external_api:games.spotguessr.sessions.summary", started["session_id"])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session_id"], started["session_id"])
        participant = body["participants"][0]
        self.assertEqual(participant["profile_slug"], self.profile.slug)
        self.assertNotIn("profile_id", participant)
        self.assertIn("rating_delta", participant)
        self.assertIn("best_round_points", participant)


class MultiplayerContainmentTests(_SpotGuessrApiTestCase):
    """Every session-scoped endpoint refuses a session this API cannot finish."""

    def setUp(self) -> None:
        """Build both an in-lobby and an already-begun multiplayer session hosted by the key owner."""
        super().setUp()
        friendship = Friendship.request(self.profile, self.other_profile)
        assert friendship is not None
        friendship.accept()
        self.lobby = start_multiplayer_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), [self.other_profile])
        self.begun = start_multiplayer_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(), [self.other_profile])
        GameSession.objects.filter(pk=self.begun.pk).update(status=GameSessionStatus.ACTIVE)

    def _assert_refused(self, response) -> None:
        """Assert a response is the machine-readable "use the web client" refusal."""
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error_code"], "multiplayer_unsupported")

    def test_the_round_endpoint_refuses_a_lobby(self) -> None:
        self._assert_refused(self._get("external_api:games.spotguessr.sessions.round", self.lobby.pk))

    def test_the_round_endpoint_refuses_a_begun_multiplayer_game(self) -> None:
        """The one that actually traps a client: it is ACTIVE, exactly like a solo game."""
        self._assert_refused(self._get("external_api:games.spotguessr.sessions.round", self.begun.pk))

    def test_the_summary_endpoint_refuses_multiplayer(self) -> None:
        self._assert_refused(self._get("external_api:games.spotguessr.sessions.summary", self.begun.pk))

    def test_the_detail_endpoint_refuses_multiplayer(self) -> None:
        self._assert_refused(self._get("external_api:games.spotguessr.sessions.detail", self.begun.pk))

    def test_the_guess_endpoint_refuses_multiplayer(self) -> None:
        round_ = GameRound.objects.create(session=self.begun, sequence_index=0, location=self.location)
        self._assert_refused(
            self._post(
                "external_api:games.spotguessr.sessions.rounds.guess",
                self.begun.pk,
                round_.pk,
                body={"latitude": 42.0, "longitude": -73.0},
            )
        )

    def test_the_round_image_endpoint_refuses_multiplayer(self) -> None:
        round_ = GameRound.objects.create(session=self.begun, sequence_index=0, location=self.location, image=self.image)
        media_key = self._key_with_scopes([*_GAME_SCOPES, ApiKeyScope.MEDIA_READ.value])
        self._assert_refused(self._get("external_api:games.spotguessr.sessions.rounds.image", self.begun.pk, round_.pk, key=media_key))

    def test_no_lobby_or_invite_route_is_exposed(self) -> None:
        """Multiplayer is not partially mirrored - there is nothing to half-use."""
        from django.urls import NoReverseMatch

        for name in ("games.spotguessr.sessions.invite", "games.spotguessr.sessions.join", "games.spotguessr.sessions.begin", "games.spotguessr.sessions.chat"):
            with self.assertRaises(NoReverseMatch):
                reverse(f"external_api:{name}", args=[self.begun.pk])


class SpotGuessrRoundImageTests(_SpotGuessrApiTestCase):
    """The round photo's bytes, which must not carry the answer in their metadata."""

    def setUp(self) -> None:
        """Give the round a real JPEG whose EXIF points straight at the answer."""
        super().setUp()
        self.session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        self.image.image.save("gps.jpg", SimpleUploadedFile("gps.jpg", _gps_jpeg_bytes(), content_type="image/jpeg"), save=True)
        self.round = GameRound.objects.create(session=self.session, sequence_index=0, location=self.location, image=self.image)
        self.media_key = self._key_with_scopes([*_GAME_SCOPES, ApiKeyScope.MEDIA_READ.value])

    def _fetch(self, key: str | None = None):
        """GET the round image with the given (default: fully scoped) key."""
        return self._get("external_api:games.spotguessr.sessions.rounds.image", self.session.pk, self.round.pk, key=key or self.media_key)

    def test_the_stored_file_really_does_carry_gps(self) -> None:
        """Guards the test itself: a fixture with no EXIF would make the next test vacuous."""
        with self.image.image.open("rb") as handle:
            stored = PILImage.open(handle)
            self.assertTrue(stored.getexif().get_ifd(0x8825))

    def test_the_served_bytes_have_no_exif_at_all(self) -> None:
        response = self._fetch()
        self.assertEqual(response.status_code, 200)
        served = PILImage.open(io.BytesIO(response.content))
        self.assertFalse(served.getexif().get_ifd(0x8825))
        self.assertFalse(dict(served.getexif()))
        self.assertNotIn(_ANSWER_NAMING_CAPTION.encode(), response.content)

    def test_the_image_is_still_the_same_picture(self) -> None:
        """Stripping metadata must not degrade the thing the player is judging."""
        served = PILImage.open(io.BytesIO(self._fetch().content))
        self.assertEqual(served.size, (16, 16))

    def test_media_read_alone_is_not_enough(self) -> None:
        media_only = self._key_with_scopes([ApiKeyScope.MEDIA_READ.value])
        self.assertEqual(self._fetch(key=media_only).status_code, 403)

    def test_games_read_alone_is_not_enough(self) -> None:
        self.assertEqual(self._fetch(key=self.raw_key).status_code, 403)

    def test_another_users_round_is_a_404(self) -> None:
        theirs = start_solo_session(self.other_profile, SpotGuessrMode.PHOTOS, GameConfig())
        their_round = GameRound.objects.create(session=theirs, sequence_index=0, location=self.location, image=self.image)
        response = self._get("external_api:games.spotguessr.sessions.rounds.image", theirs.pk, their_round.pk, key=self.media_key)
        self.assertEqual(response.status_code, 404)

    def test_a_round_with_no_photo_is_a_404(self) -> None:
        text_round = GameRound.objects.create(session=self.session, sequence_index=1, location=self.location, display_text="Old Mill House")
        response = self._get("external_api:games.spotguessr.sessions.rounds.image", self.session.pk, text_round.pk, key=self.media_key)
        self.assertEqual(response.status_code, 404)


class SpotGuessrPreferencesTests(_SpotGuessrApiTestCase):
    """Writing the one genuinely user-editable preference."""

    def test_patch_updates_show_ratings_to_friends(self) -> None:
        response = self._patch("external_api:games.spotguessr.preferences", body={"show_ratings_to_friends": False})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["show_ratings_to_friends"])
        body = self._get("external_api:games.spotguessr").json()
        self.assertFalse(body["show_ratings_to_friends"])

    def test_last_config_is_not_writable_here(self) -> None:
        response = self._patch("external_api:games.spotguessr.preferences", body={"show_ratings_to_friends": True, "last_config": {"difficulty": 0.9}})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("last_config", response.json())

    def test_needs_the_write_scope(self) -> None:
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._patch("external_api:games.spotguessr.preferences", body={"show_ratings_to_friends": False}, key=read_key)
        self.assertEqual(response.status_code, 403)

    def test_missing_field_is_a_400(self) -> None:
        response = self._patch("external_api:games.spotguessr.preferences", body={})
        self.assertEqual(response.status_code, 400)


class SpotGuessrEligibleCountTests(_SpotGuessrApiTestCase):
    """The pre-check that warns a player before they start a session with nothing to play."""

    def _polygon_around(self, location: Location) -> dict:
        lat, lng = float(location.latitude), float(location.longitude)
        return {"type": "Polygon", "coordinates": [[[lng - 0.01, lat - 0.01], [lng - 0.01, lat + 0.01], [lng + 0.01, lat + 0.01], [lng + 0.01, lat - 0.01], [lng - 0.01, lat - 0.01]]]}

    def test_counts_pins_inside_the_area(self) -> None:
        geo_bounds = json.dumps(self._polygon_around(self.location))
        response = self._get("external_api:games.spotguessr.eligible-count", data={"geo_bounds": geo_bounds})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 1)

    def test_an_area_with_nothing_pinned_counts_zero(self) -> None:
        elsewhere = baker.make(Location, latitude="10.000000", longitude="10.000000", official_name="Nowhere Near")
        geo_bounds = json.dumps(self._polygon_around(elsewhere))
        response = self._get("external_api:games.spotguessr.eligible-count", data={"geo_bounds": geo_bounds})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)

    def test_malformed_geo_bounds_is_a_400_not_a_500(self) -> None:
        response = self._get("external_api:games.spotguessr.eligible-count", data={"geo_bounds": "{not json"})
        self.assertEqual(response.status_code, 400)

    def test_geo_bounds_is_required(self) -> None:
        response = self._get("external_api:games.spotguessr.eligible-count")
        self.assertEqual(response.status_code, 400)

    def test_a_read_only_credential_is_enough(self) -> None:
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        geo_bounds = json.dumps(self._polygon_around(self.location))
        response = self._get("external_api:games.spotguessr.eligible-count", data={"geo_bounds": geo_bounds}, key=read_key)
        self.assertEqual(response.status_code, 200)


class SpotGuessrEligiblePinsTests(_SpotGuessrApiTestCase):
    """The browse feed of the caller's own pins - a read, not a play mode."""

    def setUp(self) -> None:
        """This endpoint returns pin labels/coordinates, so it needs pins:read too."""
        super().setUp()
        self.raw_key = self._key_with_scopes([*_GAME_SCOPES, ApiKeyScope.PINS_READ.value])

    def test_needs_the_pins_scope(self) -> None:
        games_only_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._get("external_api:games.spotguessr.eligible-pins", key=games_only_key)
        self.assertEqual(response.status_code, 403)

    def test_lists_the_callers_own_pins(self) -> None:
        response = self._get("external_api:games.spotguessr.eligible-pins")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["count"], 1)
        row = body["results"][0]
        self.assertAlmostEqual(row["latitude"], float(self.location.latitude), places=3)
        self.assertAlmostEqual(row["longitude"], float(self.location.longitude), places=3)
        self.assertIn("label", row)

    def test_never_lists_another_profiles_pins(self) -> None:
        other_location = self._make_location()
        baker.make(Pin, profile=self.other_profile, location=other_location)
        body = self._get("external_api:games.spotguessr.eligible-pins").json()
        self.assertEqual(body["count"], 1)

    def test_can_be_narrowed_by_geo_bounds(self) -> None:
        elsewhere = baker.make(Location, latitude="10.000000", longitude="10.000000", official_name="Nowhere Near")
        baker.make(Pin, profile=self.profile, location=elsewhere)

        lat, lng = float(self.location.latitude), float(self.location.longitude)
        polygon = {"type": "Polygon", "coordinates": [[[lng - 0.01, lat - 0.01], [lng - 0.01, lat + 0.01], [lng + 0.01, lat + 0.01], [lng + 0.01, lat - 0.01], [lng - 0.01, lat - 0.01]]]}
        body = self._get("external_api:games.spotguessr.eligible-pins", data={"geo_bounds": json.dumps(polygon)}).json()
        self.assertEqual(body["count"], 1)

    def test_malformed_geo_bounds_is_a_400_not_a_500(self) -> None:
        response = self._get("external_api:games.spotguessr.eligible-pins", data={"geo_bounds": "{not json"})
        self.assertEqual(response.status_code, 400)


class SpotGuessrRoundExpireTests(_SpotGuessrApiTestCase):
    """Force-revealing a round whose timer genuinely ran out."""

    def setUp(self) -> None:
        """Start a timed session so its round can actually expire."""
        super().setUp()
        session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig(round_time_limit_seconds=30))
        self.session_id = session.pk
        self.round = GameRound.objects.create(session=session, sequence_index=0, location=self.location)

    def test_an_expired_timer_is_revealed(self) -> None:
        GameRound.objects.filter(pk=self.round.pk).update(created=timezone.now() - timedelta(seconds=60))
        response = self._post("external_api:games.spotguessr.sessions.rounds.expire", self.session_id, self.round.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["revealed"])
        self.round.refresh_from_db()
        self.assertIsNotNone(self.round.revealed_at)

    def test_a_timer_that_has_not_expired_yet_is_a_harmless_no_op(self) -> None:
        response = self._post("external_api:games.spotguessr.sessions.rounds.expire", self.session_id, self.round.pk)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["revealed"])
        self.round.refresh_from_db()
        self.assertIsNone(self.round.revealed_at)

    def test_a_repeat_call_after_reveal_stays_a_no_op(self) -> None:
        GameRound.objects.filter(pk=self.round.pk).update(created=timezone.now() - timedelta(seconds=60))
        self._post("external_api:games.spotguessr.sessions.rounds.expire", self.session_id, self.round.pk)
        response = self._post("external_api:games.spotguessr.sessions.rounds.expire", self.session_id, self.round.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["revealed"])

    def test_another_users_session_is_a_404(self) -> None:
        theirs = start_solo_session(self.other_profile, SpotGuessrMode.PHOTOS, GameConfig(round_time_limit_seconds=30))
        their_round = GameRound.objects.create(session=theirs, sequence_index=0, location=self.location)
        response = self._post("external_api:games.spotguessr.sessions.rounds.expire", theirs.pk, their_round.pk)
        self.assertEqual(response.status_code, 404)

    def test_needs_the_write_scope(self) -> None:
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._post("external_api:games.spotguessr.sessions.rounds.expire", self.session_id, self.round.pk, key=read_key)
        self.assertEqual(response.status_code, 403)


class SpotGuessrRoundFeedbackTests(_SpotGuessrApiTestCase):
    """Reacting to the photo a Photos-mode round showed."""

    def setUp(self) -> None:
        """Start a session, guess on its round, so there is something to react to."""
        super().setUp()
        started = self._start_session()
        self.session_id = started["session_id"]
        self.round_id = started["round"]["round_id"]

    def _guess(self) -> None:
        self._post(
            "external_api:games.spotguessr.sessions.rounds.guess",
            self.session_id,
            self.round_id,
            body={"latitude": 42.0, "longitude": -73.0},
        )

    def test_thumbs_up_is_recorded(self) -> None:
        self._guess()
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": GamePhotoFeedbackKind.THUMBS_UP},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["kind"], GamePhotoFeedbackKind.THUMBS_UP)
        self.assertTrue(GamePhotoFeedback.objects.filter(round_id=self.round_id, profile=self.profile, kind=GamePhotoFeedbackKind.THUMBS_UP).exists())

    def test_a_repeat_reaction_overwrites_the_first(self) -> None:
        self._guess()
        self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": GamePhotoFeedbackKind.THUMBS_UP},
        )
        self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": GamePhotoFeedbackKind.REPORTED},
        )
        self.assertEqual(GamePhotoFeedback.objects.filter(round_id=self.round_id, profile=self.profile).count(), 1)
        self.assertEqual(GamePhotoFeedback.objects.get(round_id=self.round_id, profile=self.profile).kind, GamePhotoFeedbackKind.REPORTED)

    def test_reacting_before_guessing_is_refused(self) -> None:
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": GamePhotoFeedbackKind.THUMBS_UP},
        )
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_kind_is_a_400(self) -> None:
        self._guess()
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": "not_a_real_kind"},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_round_with_no_photo_has_nothing_to_react_to(self) -> None:
        text_session = start_solo_session(self.profile, SpotGuessrMode.NAMED_PLACE, GameConfig())
        text_round = GameRound.objects.create(session=text_session, sequence_index=0, location=self.location, display_text="Old Mill House")
        Guess.objects.create(round=text_round, profile=self.profile, guess_point=self.location.point)
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            text_session.pk,
            text_round.pk,
            body={"kind": GamePhotoFeedbackKind.THUMBS_UP},
        )
        self.assertEqual(response.status_code, 400)

    def test_needs_the_write_scope(self) -> None:
        self._guess()
        read_key = self._key_with_scopes([ApiKeyScope.GAMES_READ.value])
        response = self._post(
            "external_api:games.spotguessr.sessions.rounds.feedback",
            self.session_id,
            self.round_id,
            body={"kind": GamePhotoFeedbackKind.THUMBS_UP},
            key=read_key,
        )
        self.assertEqual(response.status_code, 403)
