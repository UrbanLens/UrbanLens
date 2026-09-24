"""The Consensus in-round photo upload endpoint's refusals and its success path (P37)."""

from __future__ import annotations

import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusFieldKind,
    ConsensusProfile,
    ConsensusRound,
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.points import PHOTO_UPLOAD_BONUS_POINTS
from urbanlens.dashboard.services.media.storage import StorageQuotaExceededError, UploadReservation


def _jpeg(colour: tuple[int, int, int] = (10, 20, 30)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=colour).save(buf, format="JPEG")
    return SimpleUploadedFile("spot.jpg", buf.getvalue(), content_type="image/jpeg")


def _points(profile: Profile) -> int:
    row = ConsensusProfile.objects.filter(profile=profile).first()
    return row.total_points if row else 0


class _UploadCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        grant_alpha_features(self.user)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.wiki = baker.make(Wiki, location=baker.make(Location))
        self.session = ConsensusSession.objects.create(host_profile=self.profile, status=ConsensusSessionStatus.ACTIVE)
        self.participant = ConsensusSessionParticipant.objects.create(
            session=self.session, profile=self.profile, status=ConsensusSessionParticipantStatus.JOINED
        )
        self.round = ConsensusRound.objects.create(
            session=self.session, sequence_index=0, wiki=self.wiki, field_kind=ConsensusFieldKind.PHOTO_COORDINATES
        )
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

    def _post(self, data: dict | None = None, round_id: int | None = None):
        return self.client.post(
            reverse("consensus.photo", args=[self.session.pk, round_id or self.round.pk]),
            data={"image": _jpeg()} if data is None else data,
        )

    def _assert_nothing_stored(self) -> None:
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())
        self.assertEqual(_points(self.profile), 0)
        self.enqueue.assert_not_called()


class RefusalTests(_UploadCase):
    def test_an_invited_participant_who_has_not_accepted_is_refused(self) -> None:
        ConsensusSessionParticipant.objects.filter(pk=self.participant.pk).update(
            status=ConsensusSessionParticipantStatus.INVITED
        )

        self.assertEqual(self._post().status_code, 403)
        self._assert_nothing_stored()

    def test_a_profile_outside_the_session_gets_a_404(self) -> None:
        ConsensusSessionParticipant.objects.filter(pk=self.participant.pk).delete()

        self.assertEqual(self._post().status_code, 404)
        self._assert_nothing_stored()

    def test_a_round_from_another_session_gets_a_404(self) -> None:
        other_session = ConsensusSession.objects.create(host_profile=self.profile, status=ConsensusSessionStatus.ACTIVE)
        other_round = ConsensusRound.objects.create(
            session=other_session, sequence_index=0, wiki=self.wiki, field_kind=ConsensusFieldKind.PHOTO_COORDINATES
        )

        self.assertEqual(self._post(round_id=other_round.pk).status_code, 404)
        self._assert_nothing_stored()

    def test_a_request_with_no_image_is_refused(self) -> None:
        self.assertEqual(self._post(data={}).status_code, 400)
        self._assert_nothing_stored()

    def test_a_file_that_is_not_an_image_is_refused(self) -> None:
        script = SimpleUploadedFile("spot.jpg", b"#!/bin/sh\necho hi\n", content_type="image/jpeg")

        self.assertEqual(self._post(data={"image": script}).status_code, 400)
        self._assert_nothing_stored()

    def test_an_upload_over_the_storage_quota_is_refused(self) -> None:
        with mock.patch.object(UploadReservation, "reserve", side_effect=StorageQuotaExceededError("over quota")):
            response = self._post()

        self.assertEqual(response.status_code, 413)
        self._assert_nothing_stored()

    def test_the_same_file_twice_is_refused_the_second_time(self) -> None:
        self.assertEqual(self._post().status_code, 201)

        response = self._post()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(_points(self.profile), PHOTO_UPLOAD_BONUS_POINTS, "the refused duplicate still paid out")

    def test_a_duplicate_at_full_quota_is_still_a_duplicate(self) -> None:
        """It stores no new bytes, so a full quota must not turn the 409 into a 413."""
        from urbanlens.dashboard.models.site_settings.model import SiteSettings
        from urbanlens.dashboard.services.media.storage import GIB

        self.assertEqual(self._post().status_code, 201)
        site = SiteSettings.get_current()
        site.storage_quota_gb = 1
        site.save()
        baker.make(Image, profile=self.profile, file_size=GIB)

        self.assertEqual(self._post().status_code, 409)

    def test_a_user_without_alpha_features_is_refused(self) -> None:
        outsider = baker.make("auth.User")
        ConsensusSessionParticipant.objects.filter(pk=self.participant.pk).update(profile=outsider.profile)
        self.client.force_login(outsider)

        self.assertEqual(self._post().status_code, 403)
        self.assertFalse(Image.objects.filter(profile=outsider.profile).exists())


class SuccessTests(_UploadCase):
    def test_the_photo_is_stored_on_the_rounds_wiki_queued_and_rewarded(self) -> None:
        response = self._post()

        self.assertEqual(response.status_code, 201)
        image = Image.objects.get(pk=response.json()["image_id"])
        self.assertEqual(image.profile_id, self.profile.pk)
        self.assertEqual(image.wiki_id, self.wiki.pk)
        self.assertTrue(image.checksum)
        self.enqueue.assert_called_once()
        self.assertEqual(self.enqueue.call_args.args[1], image.pk)
        self.assertEqual(_points(self.profile), PHOTO_UPLOAD_BONUS_POINTS)
