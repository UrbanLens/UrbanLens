"""The pre-reveal round payload must not carry anything that names the answer."""

from __future__ import annotations

from itertools import count
import json

from django.core.files.base import ContentFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.spotguessr.model import GameRound, SpotGuessrMode
from urbanlens.dashboard.services.spotguessr import serializers
from urbanlens.dashboard.services.spotguessr.session import GameConfig, start_solo_session

_coordinate_counter = count()

#: A caption of exactly the kind EXIF/IPTC hands us - it names the place outright.
_ANSWER_NAMING_CAPTION = "Old Mill House, Troy NY"


def _make_location() -> Location:
    """A location with coordinates distinct from every other one this module makes."""
    offset = next(_coordinate_counter)
    return baker.make(
        Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}", official_name="Old Mill House"
    )


def _make_profile() -> Profile:
    """A fresh profile with its auto-created user."""
    return Profile.objects.get(user=baker.make("auth.User"))


class PreRevealRoundPayloadTests(TestCase):
    """``serialize_round`` never carries the photo's caption."""

    def setUp(self) -> None:
        """Build a Photos-mode round whose image carries an answer-naming caption."""
        self.profile = _make_profile()
        self.location = _make_location()
        self.image = baker.make(
            Image,
            location=self.location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            caption=_ANSWER_NAMING_CAPTION,
            image=ContentFile(b"fake image bytes", name="test.jpg"),
        )
        self.session = start_solo_session(self.profile, SpotGuessrMode.PHOTOS, GameConfig())
        self.round = GameRound.objects.create(
            session=self.session, sequence_index=0, location=self.location, image=self.image
        )

    def test_the_photo_caption_is_absent_before_the_reveal(self) -> None:
        """The caption names the place, so it is not part of the question."""
        data = serializers.serialize_round(self.round)
        self.assertNotIn("image_caption", data)
        self.assertNotIn(_ANSWER_NAMING_CAPTION, json.dumps(data))

    def test_the_round_payload_still_carries_the_image_itself(self) -> None:
        """Dropping the caption must not drop the photo the round is *about*."""
        data = serializers.serialize_round(self.round)
        self.assertIn("image_url", data)
