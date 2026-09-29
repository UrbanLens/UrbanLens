"""Standalone-map markup edits, Vault issue dismissals, confirmed-import cancel and trivia kicks."""

from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoMetadataConflict, PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.trivia.model import (
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.services.pins.confirmed_import import (
    ConfirmedImportStatus,
    _cancel_key,
    import_cancel_requested,
)


class _Users(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class MarkupMapItemEditRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.map = baker.make(MarkupMap, profile=self.profile)
        self.item = baker.make(
            PinMarkup,
            parent_map=self.map,
            profile=self.profile,
            markup_type="polyline",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            label="Route in",
        )
        self.url = reverse("markup_map.markup.edit", args=[self.map.uuid, self.item.uuid])

    def _label(self) -> str | None:
        return PinMarkup.objects.filter(pk=self.item.pk).values_list("label", flat=True).first()

    def test_owner_relabels_then_deletes(self) -> None:
        self.client.force_login(self.user)

        edited = self.client.post(self.url, {"label": "Route out"}, content_type="application/json")
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(self._label(), "Route out")

        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.assertFalse(PinMarkup.objects.filter(pk=self.item.pk).exists())

    def test_a_stranger_can_neither_edit_nor_delete(self) -> None:
        self.client.force_login(self.stranger_user)

        edited = self.client.post(self.url, {"label": "vandal"}, content_type="application/json")
        deleted = self.client.delete(self.url)

        self.assertEqual((edited.status_code, deleted.status_code), (404, 404))
        self.assertEqual(self._label(), "Route in")

    def test_an_item_from_another_map_is_404_through_this_one(self) -> None:
        their_map = baker.make(MarkupMap, profile=self.stranger)
        theirs = baker.make(
            PinMarkup,
            parent_map=their_map,
            profile=self.stranger,
            markup_type="polyline",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        self.client.force_login(self.user)

        response = self.client.delete(reverse("markup_map.markup.edit", args=[self.map.uuid, theirs.uuid]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinMarkup.objects.filter(pk=theirs.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertTrue(PinMarkup.objects.filter(pk=self.item.pk).exists())

    def test_an_over_long_label_is_400_and_nothing_changes(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"label": "x" * 10_000}, content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._label(), "Route in")

    def test_a_form_encoded_label_is_not_a_500(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"label": "Route out"})

        self.assertLess(response.status_code, 500)


class VaultIssueDismissRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.failure = PhotoUploadFailure.objects.create(profile=self.profile, filename="IMG_1.HEIC", error="decode")
        self.conflict = baker.make(
            PhotoMetadataConflict,
            profile=self.profile,
            existing_image=baker.make(Image, profile=self.profile),
            new_image=baker.make(Image, profile=self.profile),
        )
        self.urls = {
            "failure": reverse("vault.photos.failures.dismiss", args=[self.failure.pk]),
            "conflict": reverse("vault.photos.conflicts.dismiss", args=[self.conflict.pk]),
        }

    def _statuses(self) -> tuple[str, str]:
        self.failure.refresh_from_db()
        self.conflict.refresh_from_db()
        return self.failure.status, self.conflict.status

    def test_owner_dismisses_both(self) -> None:
        self.client.force_login(self.user)

        for url in self.urls.values():
            self.assertEqual(self.client.post(url).status_code, 200, url)

        self.assertEqual(self._statuses(), (PhotoIssueStatus.DISMISSED, PhotoIssueStatus.DISMISSED))

    def test_a_stranger_gets_404_and_nothing_is_dismissed(self) -> None:
        self.client.force_login(self.stranger_user)

        for url in self.urls.values():
            self.assertEqual(self.client.post(url).status_code, 404, url)

        self.assertEqual(self._statuses(), (PhotoIssueStatus.PENDING, PhotoIssueStatus.PENDING))

    def test_anonymous_is_redirected_to_login(self) -> None:
        for url in self.urls.values():
            self.assert_login_redirect(self.client.post(url))

        self.assertEqual(self._statuses(), (PhotoIssueStatus.PENDING, PhotoIssueStatus.PENDING))


class ConfirmedImportCancelRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.job_id = str(uuid4())
        ConfirmedImportStatus(self.job_id).write("running", 10, "Importing...", user_id=self.user.pk)
        self.url = reverse("pin.import.confirmed.cancel", args=[self.job_id])
        self.addCleanup(cache.delete, _cancel_key(self.job_id))

    def _cancel_requested(self) -> bool:
        return import_cancel_requested(self.job_id)

    def test_the_importer_asks_their_import_to_stop(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 202)
        self.assertTrue(self._cancel_requested())

    def test_someone_elses_import_is_404_and_keeps_running(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._cancel_requested())

    def test_an_unknown_job_is_404(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(reverse("pin.import.confirmed.cancel", args=[str(uuid4())]))

        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_refused(self) -> None:
        response = self.client.post(self.url)

        self.assertIn(response.status_code, (302, 401, 403))
        self.assertFalse(self._cancel_requested())


class TriviaKickRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.player_user = baker.make(User)
        self.player = self.player_user.profile
        for user in (self.user, self.player_user, self.stranger_user):
            grant_alpha_features(user)
        self.session = baker.make(TriviaSession, host_profile=self.profile, status=TriviaSessionStatus.LOBBY)
        for profile in (self.profile, self.player):
            TriviaSessionParticipant.objects.create(
                session=self.session, profile=profile, status=TriviaSessionParticipantStatus.JOINED
            )
        self.url = reverse("trivia.kick", args=[self.session.pk])

    def _player_status(self) -> str:
        return TriviaSessionParticipant.objects.get(session=self.session, profile=self.player).status

    def test_the_host_removes_a_player(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"profile_id": self.player.pk})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._player_status(), TriviaSessionParticipantStatus.LEFT)

    def test_a_player_cannot_remove_the_host(self) -> None:
        self.client.force_login(self.player_user)

        response = self.client.post(self.url, {"profile_id": self.profile.pk})

        self.assertIn(response.status_code, range(400, 500))
        host = TriviaSessionParticipant.objects.get(session=self.session, profile=self.profile)
        self.assertEqual(host.status, TriviaSessionParticipantStatus.JOINED)

    def test_a_non_participant_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url, {"profile_id": self.player.pk})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._player_status(), TriviaSessionParticipantStatus.JOINED)

    def test_a_missing_or_non_numeric_profile_id_is_400(self) -> None:
        self.client.force_login(self.user)

        for body in ({}, {"profile_id": "abc"}):
            self.assertEqual(self.client.post(self.url, body).status_code, 400, body)

        self.assertEqual(self._player_status(), TriviaSessionParticipantStatus.JOINED)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"profile_id": self.player.pk}))
        self.assertEqual(self._player_status(), TriviaSessionParticipantStatus.JOINED)
