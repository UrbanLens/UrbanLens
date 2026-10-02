"""Game lobby and round routes: start, invite, join, begin, end, leave, settings, and Consensus answers and skips (P29).

Each asserts what the host or a joined player may do, that a participant without the role and a stranger are refused
with nothing changed, that anonymous is sent to log in, and that a malformed body is a 4xx.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import IndoorOutdoor
from urbanlens.dashboard.models.consensus.model import (
    ConsensusAnswer,
    ConsensusFieldKind,
    ConsensusRound,
    ConsensusRoundResolution,
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta.type import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import (
    GameSession,
    GameSessionParticipant,
    GameSessionParticipantStatus,
    GameSessionStatus,
    SpotGuessrMode,
    SpotGuessrPreference,
)
from urbanlens.dashboard.models.trivia.model import (
    TriviaPreference,
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.models.wiki.model import Wiki


def _player() -> Profile:
    user = baker.make(User)
    grant_alpha_features(user)
    return user.profile


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class _Players(TestCase):
    """A host, their friend, a non-friend and a stranger who all hold the games entitlement."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.host = _player()
        self.friend = _player()
        self.second_friend = _player()
        self.stranger = _player()
        _befriend(self.host, self.friend)
        _befriend(self.host, self.second_friend)

    def login(self, profile: Profile) -> None:
        self.client.force_login(profile.user)

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def invites_to(self, profile: Profile, kind: str) -> int:
        return NotificationLog.objects.filter(profile=profile, notification_type=kind).count()


# -- Consensus --------------------------------------------------------------------------------------------------------


class _ConsensusLobby(_Players):
    def setUp(self) -> None:
        super().setUp()
        self.session = ConsensusSession.objects.create(host_profile=self.host, status=ConsensusSessionStatus.LOBBY)
        self._participant(self.host, ConsensusSessionParticipantStatus.JOINED)
        self._participant(self.friend, ConsensusSessionParticipantStatus.INVITED)

    def _participant(self, profile: Profile, status: str) -> ConsensusSessionParticipant:
        return ConsensusSessionParticipant.objects.create(session=self.session, profile=profile, status=status)

    def url(self, name: str) -> str:
        return reverse(name, args=[self.session.pk])

    def status(self) -> str:
        self.session.refresh_from_db()
        return self.session.status

    def participant_status(self, profile: Profile) -> str | None:
        row = ConsensusSessionParticipant.objects.filter(session=self.session, profile=profile).first()
        return row.status if row else None


def _eligible_wiki(visited_by: Profile) -> Wiki:
    """A wiki the profile has visited whose only open field is its description, so round selection is deterministic."""
    location = baker.make(Location)
    wiki = baker.make(
        Wiki,
        location=location,
        name="Old Mill Sanatorium",
        description=None,
        indoor_outdoor=IndoorOutdoor.INSIDE,
        pin_type=PinType.BUILDING,
        pin_type_is_user_provided=True,
    )
    baker.make("dashboard.WikiAlias", wiki=wiki, name="The Mill")
    baker.make(Pin, profile=visited_by, location=location, last_visited=timezone.now())
    return wiki


class ConsensusStartRouteTests(_Players):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("consensus.start")
        self.enterContext(
            mock.patch("urbanlens.dashboard.services.consensus.selection.should_inject_check", return_value=False)
        )

    def test_a_solo_start_opens_an_active_session_with_its_first_round(self) -> None:
        wiki = _eligible_wiki(self.host)
        self.login(self.host)

        response = self.client.post(self.url, {"total_rounds": "4"})

        self.assertEqual(response.status_code, 200, response.content)
        session = ConsensusSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual((session.host_profile_id, session.status, session.total_rounds), (self.host.pk, "active", 4))
        self.assertEqual(ConsensusRound.objects.get(session=session).wiki_id, wiki.pk)

    def test_a_competitive_start_opens_a_lobby_and_invites_the_friends(self) -> None:
        self.login(self.host)

        response = self.client.post(self.url, {"invite_profile_ids": [self.friend.pk, self.second_friend.pk]})

        self.assertEqual(response.status_code, 200, response.content)
        session = ConsensusSession.objects.get(pk=response.json()["session_id"])
        self.assertEqual(session.status, ConsensusSessionStatus.LOBBY)
        invited = set(session.participants.filter(status="invited").values_list("profile_id", flat=True))
        self.assertEqual(invited, {self.friend.pk, self.second_friend.pk})
        self.assertEqual(self.invites_to(self.friend, NotificationType.CONSENSUS_INVITE), 1)

    def test_naming_a_non_friend_starts_nothing_and_notifies_no_one(self) -> None:
        """A refused start must not leave a lobby behind, nor invite the friends listed before the stranger."""
        self.login(self.host)

        response = self.client.post(self.url, {"invite_profile_ids": [self.friend.pk, self.stranger.pk]})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ConsensusSession.objects.exists())
        self.assertEqual(self.invites_to(self.friend, NotificationType.CONSENSUS_INVITE), 0)

    def test_a_malformed_invitee_id_is_400_and_starts_nothing(self) -> None:
        self.login(self.host)

        for ids in (["abc"], [str(self.friend.pk), "1.5"], ["99999999"]):
            response = self.client.post(self.url, {"invite_profile_ids": ids})
            self.assertEqual(response.status_code, 400, ids)
        self.assertFalse(ConsensusSession.objects.exists())

    def test_someone_without_the_games_entitlement_is_refused(self) -> None:
        self.client.force_login(baker.make(User))

        self.assertEqual(self.client.post(self.url, {}).status_code, 403)
        self.assertFalse(ConsensusSession.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {}))
        self.assertFalse(ConsensusSession.objects.exists())


class ConsensusInviteRouteTests(_ConsensusLobby):
    def test_the_host_invites_a_friend(self) -> None:
        self.login(self.host)

        response = self.client.post(self.url("consensus.invite"), {"profile_id": self.second_friend.pk})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.participant_status(self.second_friend), ConsensusSessionParticipantStatus.INVITED)
        self.assertEqual(self.invites_to(self.second_friend, NotificationType.CONSENSUS_INVITE), 1)

    def test_a_player_who_is_not_the_host_cannot_invite(self) -> None:
        ConsensusSessionParticipant.objects.filter(session=self.session, profile=self.friend).update(status="joined")
        _befriend(self.friend, self.stranger)
        self.login(self.friend)

        response = self.client.post(self.url("consensus.invite"), {"profile_id": self.stranger.pk})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.participant_status(self.stranger))

    def test_a_non_friend_cannot_be_invited(self) -> None:
        self.login(self.host)

        self.assertEqual(
            self.client.post(self.url("consensus.invite"), {"profile_id": self.stranger.pk}).status_code, 400
        )
        self.assertIsNone(self.participant_status(self.stranger))

    def test_an_unknown_profile_is_answered_like_a_non_friend(self) -> None:
        """Otherwise the reply says which profile ids exist."""
        self.login(self.host)

        unknown = self.client.post(self.url("consensus.invite"), {"profile_id": 99999999})
        stranger = self.client.post(self.url("consensus.invite"), {"profile_id": self.stranger.pk})

        self.assertEqual((unknown.status_code, unknown.json()), (stranger.status_code, stranger.json()))

    def test_no_invites_once_the_game_has_begun(self) -> None:
        ConsensusSession.objects.filter(pk=self.session.pk).update(status=ConsensusSessionStatus.ACTIVE)
        self.login(self.host)

        self.assertEqual(
            self.client.post(self.url("consensus.invite"), {"profile_id": self.second_friend.pk}).status_code, 400
        )
        self.assertIsNone(self.participant_status(self.second_friend))

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(
            self.client.post(self.url("consensus.invite"), {"profile_id": self.second_friend.pk}).status_code, 404
        )
        self.assertIsNone(self.participant_status(self.second_friend))

    def test_a_malformed_profile_id_is_400(self) -> None:
        self.login(self.host)
        for body in ({}, {"profile_id": "abc"}, {"profile_id": "1.5"}):
            self.assertEqual(self.client.post(self.url("consensus.invite"), body).status_code, 400, body)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(
            self.client.post(self.url("consensus.invite"), {"profile_id": self.second_friend.pk})
        )
        self.assertIsNone(self.participant_status(self.second_friend))


class ConsensusJoinRouteTests(_ConsensusLobby):
    def test_an_invitee_joins_the_lobby(self) -> None:
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("consensus.join")).status_code, 200)
        self.assertEqual(self.participant_status(self.friend), ConsensusSessionParticipantStatus.JOINED)

    def test_an_invitee_cannot_join_once_the_game_has_begun(self) -> None:
        ConsensusSession.objects.filter(pk=self.session.pk).update(status=ConsensusSessionStatus.ACTIVE)
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("consensus.join")).status_code, 400)
        self.assertEqual(self.participant_status(self.friend), ConsensusSessionParticipantStatus.INVITED)

    def test_someone_who_was_not_invited_gets_404_and_is_not_added(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("consensus.join")).status_code, 404)
        self.assertIsNone(self.participant_status(self.stranger))

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("consensus.join")))
        self.assertEqual(self.participant_status(self.friend), ConsensusSessionParticipantStatus.INVITED)


class ConsensusBeginRouteTests(_ConsensusLobby):
    def test_the_host_begins_the_game(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("consensus.begin")).status_code, 200)
        self.assertEqual(self.status(), ConsensusSessionStatus.ACTIVE)

    def test_an_invitee_cannot_begin_it(self) -> None:
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("consensus.begin")).status_code, 400)
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("consensus.begin")).status_code, 404)
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("consensus.begin")))
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)


class ConsensusEndRouteTests(_ConsensusLobby):
    def test_the_host_ends_the_game(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("consensus.end")).status_code, 200)
        self.assertEqual(self.status(), ConsensusSessionStatus.COMPLETED)

    def test_ending_it_twice_is_400(self) -> None:
        self.login(self.host)
        self.client.post(self.url("consensus.end"))

        self.assertEqual(self.client.post(self.url("consensus.end")).status_code, 400)

    def test_a_joined_player_who_is_not_the_host_cannot_end_it(self) -> None:
        ConsensusSessionParticipant.objects.filter(session=self.session, profile=self.friend).update(status="joined")
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("consensus.end")).status_code, 400)
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("consensus.end")).status_code, 404)
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("consensus.end")))
        self.assertEqual(self.status(), ConsensusSessionStatus.LOBBY)


class _ConsensusRound(_Players):
    """A solo game in progress on a wiki whose description is open, plus a lobby the friend was only invited to."""

    field_kind = ConsensusFieldKind.WIKI_DESCRIPTION

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill", description=None)
        self.session = ConsensusSession.objects.create(host_profile=self.host, status=ConsensusSessionStatus.ACTIVE)
        ConsensusSessionParticipant.objects.create(
            session=self.session, profile=self.host, status=ConsensusSessionParticipantStatus.JOINED
        )
        self.round = ConsensusRound.objects.create(
            session=self.session, sequence_index=0, wiki=self.wiki, field_kind=self.field_kind
        )

    def round_url(self, name: str, round_: ConsensusRound | None = None) -> str:
        round_ = round_ or self.round
        return reverse(name, args=[round_.session_id, round_.pk])

    def answers(self) -> int:
        return ConsensusAnswer.objects.filter(round=self.round).count()

    def description(self) -> str | None:
        self.wiki.refresh_from_db()
        return self.wiki.description

    def invite_friend(self) -> None:
        ConsensusSessionParticipant.objects.create(
            session=self.session, profile=self.friend, status=ConsensusSessionParticipantStatus.INVITED
        )

    def end_as_host(self) -> None:
        self.login(self.host)
        self.assertEqual(self.client.post(reverse("consensus.end", args=[self.session.pk])).status_code, 200)


class ConsensusAnswerRouteTests(_ConsensusRound):
    def test_a_solo_player_answers_and_the_wiki_takes_the_answer(self) -> None:
        self.login(self.host)

        response = self.client.post(self.round_url("consensus.answer"), {"value": "A brick mill on the river."})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.description(), "A brick mill on the river.")
        self.round.refresh_from_db()
        self.assertEqual(self.round.resolution, ConsensusRoundResolution.AGREED)

    def test_an_invitee_who_has_not_joined_cannot_answer(self) -> None:
        self.invite_friend()
        self.login(self.friend)

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": "Mine"}).status_code, 403)
        self.assertEqual(self.answers(), 0)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": "Mine"}).status_code, 404)
        self.assertEqual(self.answers(), 0)

    def test_a_round_of_another_session_is_404(self) -> None:
        other = ConsensusSession.objects.create(host_profile=self.stranger, status=ConsensusSessionStatus.ACTIVE)
        foreign = ConsensusRound.objects.create(
            session=other, sequence_index=0, wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_DESCRIPTION
        )
        self.login(self.host)

        url = reverse("consensus.answer", args=[self.session.pk, foreign.pk])
        self.assertEqual(self.client.post(url, {"value": "Mine"}).status_code, 404)
        self.assertFalse(ConsensusAnswer.objects.filter(round=foreign).exists())

    def test_an_empty_answer_is_400(self) -> None:
        self.login(self.host)

        for body in ({}, {"value": "   "}):
            self.assertEqual(self.client.post(self.round_url("consensus.answer"), body).status_code, 400, body)
        self.assertEqual(self.answers(), 0)

    def test_an_answer_longer_than_it_can_be_stored_is_400(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": "x" * 501}).status_code, 400)
        self.assertEqual(self.answers(), 0)
        self.assertIsNone(self.description())

    def test_an_answer_that_exactly_fits_is_taken(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": "x" * 500}).status_code, 200)
        self.assertEqual(self.description(), "x" * 500)

    def test_the_round_of_a_game_the_host_ended_takes_no_answer(self) -> None:
        """Ending a round nobody answered abandons the game; its open round must not keep accepting answers."""
        self.end_as_host()

        response = self.client.post(self.round_url("consensus.answer"), {"value": "Too late"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.answers(), 0)
        self.assertIsNone(self.description())
        self.assertEqual(ConsensusRound.objects.filter(session=self.session).count(), 1)

    def test_a_game_the_host_ended_deals_no_further_round(self) -> None:
        ConsensusRound.objects.filter(pk=self.round.pk).update(resolution=ConsensusRoundResolution.SKIPPED)
        _eligible_wiki(self.host)
        self.end_as_host()

        with mock.patch("urbanlens.dashboard.services.consensus.selection.should_inject_check", return_value=False):
            self.client.get(reverse("consensus.round", args=[self.session.pk]))

        self.assertEqual(ConsensusRound.objects.filter(session=self.session).count(), 1)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.round_url("consensus.answer"), {"value": "Mine"}))
        self.assertEqual(self.answers(), 0)


class ConsensusNameAnswerRouteTests(_ConsensusRound):
    field_kind = ConsensusFieldKind.WIKI_NAME

    def test_a_name_longer_than_a_wiki_name_is_400_and_leaves_the_round_open(self) -> None:
        """It fits the answer column but not the wiki's, so accepting it wedged the round on a failed save."""
        self.login(self.host)

        response = self.client.post(self.round_url("consensus.answer"), {"value": "Mill " * 60})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.answers(), 0)
        self.round.refresh_from_db()
        self.assertEqual(self.round.resolution, ConsensusRoundResolution.PENDING)

    def test_a_name_that_exactly_fits_is_taken(self) -> None:
        self.login(self.host)
        name = "Riverside Mill " + "M" * 240

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": name}).status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, name)


class ConsensusChoiceAnswerRouteTests(_ConsensusRound):
    field_kind = ConsensusFieldKind.WIKI_INDOOR_OUTDOOR

    def test_a_value_outside_the_choices_is_400(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.round_url("consensus.answer"), {"value": "sideways"}).status_code, 400)
        self.assertEqual(self.answers(), 0)


class ConsensusPhotoAnswerRouteTests(_ConsensusRound):
    field_kind = ConsensusFieldKind.PHOTO_COORDINATES

    def setUp(self) -> None:
        super().setUp()
        uploader = baker.make(User).profile
        self.photo = baker.make(Image, profile=uploader, wiki=self.wiki, latitude=None, longitude=None)
        ConsensusRound.objects.filter(pk=self.round.pk).update(target_image=self.photo)

    def _coordinates(self) -> tuple:
        self.photo.refresh_from_db()
        return self.photo.latitude, self.photo.longitude

    def test_a_guess_places_the_photo(self) -> None:
        self.login(self.host)

        response = self.client.post(self.round_url("consensus.answer"), {"latitude": "42.5", "longitude": "-73.25"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(tuple(float(v) for v in self._coordinates()), (42.5, -73.25))

    def test_a_guess_on_a_wrapped_copy_of_the_world_is_folded_onto_it(self) -> None:
        self.login(self.host)

        response = self.client.post(self.round_url("consensus.answer"), {"latitude": "42.5", "longitude": "190"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(tuple(float(v) for v in self._coordinates()), (42.5, -170.0))

    def test_coordinates_off_the_globe_or_not_numbers_are_400(self) -> None:
        self.login(self.host)

        for latitude, longitude in (("91", "0"), ("-90.5", "0"), ("nan", "0"), ("0", "inf"), ("x", "0"), ("0", "")):
            body = {"latitude": latitude, "longitude": longitude}
            self.assertEqual(self.client.post(self.round_url("consensus.answer"), body).status_code, 400, body)
        self.assertEqual(self.answers(), 0)
        self.assertEqual(self._coordinates(), (None, None))


class ConsensusSkipRouteTests(_ConsensusRound):
    def test_a_player_skips_and_the_wiki_is_untouched(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.round_url("consensus.skip")).status_code, 200)
        self.round.refresh_from_db()
        self.assertEqual(self.round.resolution, ConsensusRoundResolution.SKIPPED)
        self.assertIsNone(self.description())

    def test_an_invitee_who_has_not_joined_cannot_skip(self) -> None:
        self.invite_friend()
        self.login(self.friend)

        self.assertEqual(self.client.post(self.round_url("consensus.skip")).status_code, 403)
        self.assertEqual(self.answers(), 0)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.round_url("consensus.skip")).status_code, 404)
        self.assertEqual(self.answers(), 0)

    def test_the_round_of_a_game_the_host_ended_cannot_be_skipped(self) -> None:
        self.end_as_host()

        self.assertEqual(self.client.post(self.round_url("consensus.skip")).status_code, 400)
        self.assertEqual(self.answers(), 0)
        self.assertEqual(ConsensusRound.objects.filter(session=self.session).count(), 1)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.round_url("consensus.skip")))
        self.assertEqual(self.answers(), 0)


# -- Trivia -----------------------------------------------------------------------------------------------------------


class _TriviaLobby(_Players):
    def setUp(self) -> None:
        super().setUp()
        self.session = TriviaSession.objects.create(host_profile=self.host, status=TriviaSessionStatus.LOBBY)
        TriviaSessionParticipant.objects.create(
            session=self.session, profile=self.host, status=TriviaSessionParticipantStatus.JOINED
        )
        TriviaSessionParticipant.objects.create(
            session=self.session, profile=self.friend, status=TriviaSessionParticipantStatus.INVITED
        )

    def url(self, name: str) -> str:
        return reverse(name, args=[self.session.pk])

    def status(self) -> str:
        self.session.refresh_from_db()
        return self.session.status

    def participant_status(self, profile: Profile) -> str | None:
        row = TriviaSessionParticipant.objects.filter(session=self.session, profile=profile).first()
        return row.status if row else None

    def join(self, profile: Profile) -> None:
        TriviaSessionParticipant.objects.update_or_create(
            session=self.session, profile=profile, defaults={"status": TriviaSessionParticipantStatus.JOINED}
        )


class TriviaSettingsRouteTests(_Players):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("trivia.settings")

    def _shown(self) -> bool | None:
        preference = TriviaPreference.objects.filter(profile=self.host).first()
        return preference.show_ratings_to_friends if preference else None

    def test_a_player_turns_rating_sharing_off_and_on(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url, {"show_ratings_to_friends": "off"}).status_code, 200)
        self.assertIs(self._shown(), False)
        self.client.post(self.url, {"show_ratings_to_friends": "on"})
        self.assertIs(self._shown(), True)

    def test_a_missing_or_garbled_value_is_400_and_keeps_the_setting(self) -> None:
        """The toggle always sends "on" or "off", so anything else is a garbled request, not a request to stop sharing."""
        self.login(self.host)
        self.client.post(self.url, {"show_ratings_to_friends": "on"})

        for body in ({}, {"show_ratings_to_friends": "true"}, {"show_ratings_to_friends": ""}):
            self.assertEqual(self.client.post(self.url, body).status_code, 400, body)
        self.assertIs(self._shown(), True)

    def test_someone_without_the_games_entitlement_is_refused(self) -> None:
        user = baker.make(User)
        self.client.force_login(user)

        self.assertEqual(self.client.post(self.url, {"show_ratings_to_friends": "off"}).status_code, 403)
        self.assertFalse(TriviaPreference.objects.filter(profile=user.profile).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"show_ratings_to_friends": "off"}))


class TriviaInviteRouteTests(_TriviaLobby):
    def test_the_host_invites_a_friend(self) -> None:
        self.login(self.host)

        self.assertEqual(
            self.client.post(self.url("trivia.invite"), {"profile_id": self.second_friend.pk}).status_code, 200
        )
        self.assertEqual(self.participant_status(self.second_friend), TriviaSessionParticipantStatus.INVITED)
        self.assertEqual(self.invites_to(self.second_friend, NotificationType.TRIVIA_INVITE), 1)

    def test_the_host_can_reinvite_a_friend_who_left(self) -> None:
        TriviaSessionParticipant.objects.filter(session=self.session, profile=self.friend).update(status="left")
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("trivia.invite"), {"profile_id": self.friend.pk}).status_code, 200)
        self.assertEqual(self.participant_status(self.friend), TriviaSessionParticipantStatus.INVITED)

    def test_a_player_who_is_not_the_host_cannot_invite(self) -> None:
        self.join(self.friend)
        _befriend(self.friend, self.stranger)
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("trivia.invite"), {"profile_id": self.stranger.pk}).status_code, 400)
        self.assertIsNone(self.participant_status(self.stranger))

    def test_a_non_friend_cannot_be_invited(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("trivia.invite"), {"profile_id": self.stranger.pk}).status_code, 400)
        self.assertIsNone(self.participant_status(self.stranger))

    def test_an_unknown_profile_is_answered_like_a_non_friend(self) -> None:
        """Otherwise the reply says which profile ids exist."""
        self.login(self.host)

        unknown = self.client.post(self.url("trivia.invite"), {"profile_id": 99999999})
        stranger = self.client.post(self.url("trivia.invite"), {"profile_id": self.stranger.pk})

        self.assertEqual((unknown.status_code, unknown.json()), (stranger.status_code, stranger.json()))

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(
            self.client.post(self.url("trivia.invite"), {"profile_id": self.second_friend.pk}).status_code, 404
        )
        self.assertIsNone(self.participant_status(self.second_friend))

    def test_a_malformed_profile_id_is_400(self) -> None:
        self.login(self.host)
        for body in ({}, {"profile_id": "abc"}):
            self.assertEqual(self.client.post(self.url("trivia.invite"), body).status_code, 400, body)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("trivia.invite"), {"profile_id": self.second_friend.pk}))
        self.assertIsNone(self.participant_status(self.second_friend))


class TriviaBeginRouteTests(_TriviaLobby):
    def test_the_host_begins_the_game(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("trivia.begin")).status_code, 200)
        self.assertEqual(self.status(), TriviaSessionStatus.ACTIVE)

    def test_a_joined_player_who_is_not_the_host_cannot_begin_it(self) -> None:
        self.join(self.friend)
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("trivia.begin")).status_code, 400)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("trivia.begin")).status_code, 404)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("trivia.begin")))
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)


class TriviaEndRouteTests(_TriviaLobby):
    def test_the_host_ends_the_game(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("trivia.end")).status_code, 200)
        self.assertEqual(self.status(), TriviaSessionStatus.COMPLETED)

    def test_a_joined_player_who_is_not_the_host_cannot_end_it(self) -> None:
        self.join(self.friend)
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("trivia.end")).status_code, 400)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_a_player_who_left_gets_404(self) -> None:
        TriviaSessionParticipant.objects.filter(session=self.session, profile=self.friend).update(status="left")
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("trivia.end")).status_code, 404)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("trivia.end")).status_code, 404)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("trivia.end")))
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)


class TriviaLeaveRouteTests(_TriviaLobby):
    def test_an_invitee_declines(self) -> None:
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url("trivia.leave")).status_code, 200)
        self.assertEqual(self.participant_status(self.friend), TriviaSessionParticipantStatus.LEFT)
        self.assertEqual(self.status(), TriviaSessionStatus.LOBBY)

    def test_the_host_leaving_hands_the_game_to_a_joined_player(self) -> None:
        self.join(self.friend)
        self.login(self.host)

        self.assertEqual(self.client.post(self.url("trivia.leave")).status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.host_profile_id, self.friend.pk)
        self.assertEqual(self.participant_status(self.host), TriviaSessionParticipantStatus.LEFT)

    def test_a_player_who_already_left_gets_404(self) -> None:
        self.login(self.friend)
        self.client.post(self.url("trivia.leave"))

        self.assertEqual(self.client.post(self.url("trivia.leave")).status_code, 404)

    def test_someone_outside_the_session_gets_404_and_nobody_leaves(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url("trivia.leave")).status_code, 404)
        self.assertEqual(self.participant_status(self.friend), TriviaSessionParticipantStatus.INVITED)
        self.assertEqual(self.participant_status(self.host), TriviaSessionParticipantStatus.JOINED)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url("trivia.leave")))
        self.assertEqual(self.participant_status(self.friend), TriviaSessionParticipantStatus.INVITED)


# -- SpotGuessr -------------------------------------------------------------------------------------------------------


class SpotGuessrInviteRouteTests(_Players):
    def setUp(self) -> None:
        super().setUp()
        self.session = GameSession.objects.create(
            host_profile=self.host, mode=SpotGuessrMode.PHOTOS, status=GameSessionStatus.LOBBY
        )
        GameSessionParticipant.objects.create(
            session=self.session, profile=self.host, status=GameSessionParticipantStatus.JOINED
        )
        GameSessionParticipant.objects.create(
            session=self.session, profile=self.friend, status=GameSessionParticipantStatus.JOINED
        )
        self.url = reverse("spotguessr.invite", args=[self.session.pk])

    def _status(self, profile: Profile) -> str | None:
        row = GameSessionParticipant.objects.filter(session=self.session, profile=profile).first()
        return row.status if row else None

    def test_the_host_invites_a_friend(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url, {"profile_id": self.second_friend.pk}).status_code, 200)
        self.assertEqual(self._status(self.second_friend), GameSessionParticipantStatus.INVITED)
        self.assertEqual(self.invites_to(self.second_friend, NotificationType.SPOTGUESSR_INVITE), 1)

    def test_a_player_who_is_not_the_host_cannot_invite(self) -> None:
        _befriend(self.friend, self.stranger)
        self.login(self.friend)

        self.assertEqual(self.client.post(self.url, {"profile_id": self.stranger.pk}).status_code, 400)
        self.assertIsNone(self._status(self.stranger))

    def test_a_non_friend_cannot_be_invited(self) -> None:
        self.login(self.host)

        self.assertEqual(self.client.post(self.url, {"profile_id": self.stranger.pk}).status_code, 400)
        self.assertIsNone(self._status(self.stranger))

    def test_an_unknown_profile_is_answered_like_a_non_friend(self) -> None:
        self.login(self.host)

        unknown = self.client.post(self.url, {"profile_id": 99999999})
        stranger = self.client.post(self.url, {"profile_id": self.stranger.pk})

        self.assertEqual((unknown.status_code, unknown.json()), (stranger.status_code, stranger.json()))

    def test_no_invites_once_the_game_has_begun(self) -> None:
        GameSession.objects.filter(pk=self.session.pk).update(status=GameSessionStatus.ACTIVE)
        self.login(self.host)

        self.assertEqual(self.client.post(self.url, {"profile_id": self.second_friend.pk}).status_code, 400)
        self.assertIsNone(self._status(self.second_friend))

    def test_someone_outside_the_session_gets_404(self) -> None:
        self.login(self.stranger)

        self.assertEqual(self.client.post(self.url, {"profile_id": self.second_friend.pk}).status_code, 404)
        self.assertIsNone(self._status(self.second_friend))

    def test_a_malformed_profile_id_is_400(self) -> None:
        self.login(self.host)
        for body in ({}, {"profile_id": "abc"}):
            self.assertEqual(self.client.post(self.url, body).status_code, 400, body)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"profile_id": self.second_friend.pk}))
        self.assertIsNone(self._status(self.second_friend))


# -- Multiplayer starts and the ratings toggle of the other two games -------------------------------------------------


class _MultiplayerStartCase(_Players):
    """``consensus.start``'s invite checks, run against the other two games' start routes."""

    __test__ = False
    route: str
    session_model: type
    invite_kind: str

    def _start(self, ids: list):
        return self.client.post(reverse(self.route), {"invite_profile_ids": ids})

    def test_a_multiplayer_start_opens_a_lobby_and_invites_the_friend(self) -> None:
        self.login(self.host)

        response = self._start([self.friend.pk])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.session_model.objects.get().status, "lobby")
        self.assertEqual(self.invites_to(self.friend, self.invite_kind), 1)

    def test_naming_a_non_friend_starts_nothing_and_notifies_no_one(self) -> None:
        self.login(self.host)

        response = self._start([self.friend.pk, self.stranger.pk])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.session_model.objects.exists())
        self.assertEqual(self.invites_to(self.friend, self.invite_kind), 0)

    def test_a_malformed_invitee_id_is_400_and_starts_nothing(self) -> None:
        self.login(self.host)

        for ids in (["abc"], ["99999999"]):
            self.assertEqual(self._start(ids).status_code, 400, ids)
        self.assertFalse(self.session_model.objects.exists())


class TriviaStartRouteTests(_MultiplayerStartCase):
    __test__ = True
    route = "trivia.start"
    session_model = TriviaSession
    invite_kind = NotificationType.TRIVIA_INVITE


class SpotGuessrStartRouteTests(_MultiplayerStartCase):
    __test__ = True
    route = "spotguessr.start"
    session_model = GameSession
    invite_kind = NotificationType.SPOTGUESSR_INVITE


class SpotGuessrSettingsRouteTests(_Players):
    def test_a_missing_or_garbled_value_is_400_and_keeps_the_setting(self) -> None:
        url = reverse("spotguessr.settings")
        self.login(self.host)
        self.assertEqual(self.client.post(url, {"show_ratings_to_friends": "on"}).status_code, 200)

        for body in ({}, {"show_ratings_to_friends": "true"}):
            self.assertEqual(self.client.post(url, body).status_code, 400, body)
        self.assertTrue(SpotGuessrPreference.objects.get(profile=self.host).show_ratings_to_friends)
