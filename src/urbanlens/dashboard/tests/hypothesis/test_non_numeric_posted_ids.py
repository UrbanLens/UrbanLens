"""A non-numeric id posted to a write route is refused, never a 500.

Django raises ``ValueError`` for ``filter(pk="abc")`` while building the query, before any "not found"
branch the view has, so every one of these answered a hand-edited or hostile form with a server error.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusRound,
    ConsensusRoundResolution,
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionParticipantStatus,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.custom_fields.model import CustomField
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki

_NOT_A_NUMBER = "abc"
_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]


class _LoggedInCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def assertRefused(self, response) -> None:  # noqa: N802
        self.assertLess(response.status_code, 500, "a non-numeric id crashed the view")


class SharingTests(_LoggedInCase):
    def test_sending_a_pin_share(self) -> None:
        pin = baker.make(Pin, profile=self.profile)

        self.assertRefused(
            self.client.post(reverse("pin.share.send", kwargs={"pin_slug": pin.slug}), {"profile_id": _NOT_A_NUMBER})
        )

    def test_sending_a_markup_map_share(self) -> None:
        markup_map = MarkupMap.objects.create(
            profile=self.profile, center_latitude=40.0, center_longitude=-74.0, zoom=16
        )

        self.assertRefused(
            self.client.post(
                reverse("markup_map.share.send", kwargs={"map_uuid": markup_map.uuid}), {"profile_id": _NOT_A_NUMBER}
            )
        )


class CustomFieldTests(_LoggedInCase):
    def test_saving_a_photo_field(self) -> None:
        image = baker.make(Image, profile=self.profile)

        self.assertRefused(
            self.client.post(reverse("custom_fields.photo", args=[image.pk]), {"field_id": _NOT_A_NUMBER, "value": "x"})
        )

    def test_saving_a_markup_map_field(self) -> None:
        markup_map = baker.make(MarkupMap, profile=self.profile)

        self.assertRefused(
            self.client.post(
                reverse("custom_fields.markup_map", args=[markup_map.uuid]), {"field_id": _NOT_A_NUMBER, "value": "x"}
            )
        )

    def test_an_id_beyond_the_integer_column_range(self) -> None:
        image = baker.make(Image, profile=self.profile)

        self.assertRefused(
            self.client.post(reverse("custom_fields.photo", args=[image.pk]), {"field_id": "9" * 30, "value": "x"})
        )

    def test_a_real_field_still_saves(self) -> None:
        image = baker.make(Image, profile=self.profile)
        field = CustomField.objects.create(profile=self.profile, entity_type="photo", name="Film stock")

        response = self.client.post(
            reverse("custom_fields.photo", args=[image.pk]), {"field_id": str(field.pk), "value": "Portra"}
        )

        self.assertEqual(response.status_code, 200)


class LabelMergeTests(_LoggedInCase):
    def test_merging_into_a_non_numeric_target(self) -> None:
        source = baker.make(Label, profile=self.profile, kind="tag", name="Source")

        self.assertRefused(
            self.client.post(
                reverse("label.merge", kwargs={"label_kind": "tag", "label_id": source.pk}),
                {"target_label_id": _NOT_A_NUMBER},
            )
        )

    def _multi_merge(self, body: str):
        return self.client.post(
            reverse("label.multi_merge", kwargs={"label_kind": "tag"}), body, content_type="application/json"
        )

    def test_multi_merging_into_an_infinite_target(self) -> None:
        # json.loads accepts the bare Infinity literal, and int(float("inf")) raises OverflowError.
        self.assertEqual(self._multi_merge('{"target_id": Infinity, "source_ids": [1]}').status_code, 400)

    def test_multi_merging_with_a_body_that_is_not_an_object(self) -> None:
        self.assertEqual(self._multi_merge("[1, 2]").status_code, 400)


class CommentTests(_LoggedInCase):
    def test_replying_on_a_pin(self) -> None:
        pin = baker.make(Pin, profile=self.profile)

        self.assertRefused(
            self.client.post(
                reverse("pin.comments", kwargs={"pin_slug": pin.slug}), {"text": "hi", "parent_id": _NOT_A_NUMBER}
            )
        )

    def test_attaching_a_photo_on_a_pin(self) -> None:
        pin = baker.make(Pin, profile=self.profile)

        self.assertRefused(
            self.client.post(
                reverse("pin.comments", kwargs={"pin_slug": pin.slug}),
                {"text": "hi", "existing_image_id": _NOT_A_NUMBER},
            )
        )

    def test_replying_on_a_wiki(self) -> None:
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location)

        self.assertRefused(
            self.client.post(
                reverse("location.wiki.comments", args=[location.slug]), {"text": "hi", "parent_id": _NOT_A_NUMBER}
            )
        )

    def test_attaching_a_photo_on_a_wiki(self) -> None:
        location = baker.make(Location)
        baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.profile, location=location)

        self.assertRefused(
            self.client.post(
                reverse("location.wiki.comments", args=[location.slug]),
                {"text": "hi", "existing_image_id": _NOT_A_NUMBER},
            )
        )

    def _trip(self) -> Trip:
        trip = baker.make(Trip, creator=self.profile, name="Shared Trip")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        return trip

    def test_replying_on_a_trip(self) -> None:
        trip = self._trip()

        self.assertRefused(
            self.client.post(reverse("trips.comments", args=[trip.slug]), {"text": "hi", "parent_id": _NOT_A_NUMBER})
        )

    def test_attaching_a_photo_on_a_trip(self) -> None:
        trip = self._trip()

        self.assertRefused(
            self.client.post(
                reverse("trips.comments", args=[trip.slug]), {"text": "hi", "existing_image_id": _NOT_A_NUMBER}
            )
        )


class OverlayTests(_LoggedInCase):
    def test_picking_a_non_numeric_photo(self) -> None:
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)

        self.assertRefused(
            self.client.post(
                reverse("pin.overlays", args=[pin.slug]), {"corners": json.dumps(_CORNERS), "image_id": _NOT_A_NUMBER}
            )
        )


class ConsensusVoteTests(_LoggedInCase):
    def test_voting_for_a_non_numeric_answer(self) -> None:
        grant_alpha_features(self.user)
        session = ConsensusSession.objects.create(host_profile=self.profile, status=ConsensusSessionStatus.ACTIVE)
        ConsensusSessionParticipant.objects.create(
            session=session, profile=self.profile, status=ConsensusSessionParticipantStatus.JOINED
        )
        round_ = baker.make(ConsensusRound, session=session, resolution=ConsensusRoundResolution.VOTE_OPEN)

        self.assertRefused(
            self.client.post(
                reverse("consensus.vote", kwargs={"session_id": session.pk, "round_id": round_.pk}),
                {"answer_id": _NOT_A_NUMBER},
            )
        )
