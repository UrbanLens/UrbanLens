"""Re-running keyword generation for an image must not crash on its own rows.

``generate_keywords_for_image`` replaces a provider's keywords by deleting the old
rows and inserting the new ones. The delete and the insert are not isolated from
another worker doing the same thing: ``uq_image_keyword`` then rejects the second
insert.

The only caller is the Celery task ``generate_photo_keywords``, and Celery delivers at
least once - a worker lost mid-task has its message redelivered, so two runs for one
image is an ordinary occurrence rather than a rare interleaving.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.keyword import ImageKeyword
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.photos.photo_keywords import KeywordResult, generate_keywords_for_image


class _StubProvider:
    """One photo-keyword provider returning a fixed set of keywords."""

    slug = "stub"

    def __init__(self, keywords: list[str]):
        self._keywords = keywords

    def is_available_for(self, _image) -> bool:
        return True

    def generate(self, _image) -> list[KeywordResult]:
        return [KeywordResult(keyword=word, confidence=0.9) for word in self._keywords]


class PhotoKeywordRerunTests(TestCase):
    """A second run over the same image is a replace, not a crash."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.location = Location.objects.create(latitude=44.4, longitude=-71.1)
        self.pin = Pin.objects.create(profile=self.profile, location=self.location, name="Keyworded")
        self.image = Image.objects.create(
            pin=self.pin, location=self.location, profile=self.profile, image="photos/k.jpg"
        )

    def _run(self, keywords: list[str]) -> dict:
        provider = _StubProvider(keywords)
        with mock.patch(
            "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=[provider]
        ):
            return generate_keywords_for_image(self.image.pk)

    def _stored(self) -> set[str]:
        return set(ImageKeyword.objects.filter(image=self.image, source="stub").values_list("keyword", flat=True))

    def test_the_first_run_stores_the_keywords(self):
        self._run(["ruin", "brick"])
        self.assertEqual(self._stored(), {"ruin", "brick"})

    def test_a_second_run_replaces_them(self):
        self._run(["ruin", "brick"])
        self._run(["ruin", "asylum"])
        self.assertEqual(self._stored(), {"ruin", "asylum"})

    def test_a_concurrent_rerun_does_not_raise(self):
        # Patching the delete away reproduces the window between one worker's
        # delete and its insert, during which another worker's rows land. Real
        # concurrency is not reachable from a single-connection test.
        self._run(["ruin", "brick"])

        with mock.patch.object(ImageKeyword.objects, "filter", side_effect=lambda **_: ImageKeyword.objects.none()):
            counts = self._run(["ruin", "asylum"])

        self.assertEqual(counts.get("stub"), 2)

    def test_the_rows_are_not_duplicated_by_a_concurrent_rerun(self):
        self._run(["ruin"])

        with mock.patch.object(ImageKeyword.objects, "filter", side_effect=lambda **_: ImageKeyword.objects.none()):
            self._run(["ruin"])

        self.assertEqual(ImageKeyword.objects.filter(image=self.image, source="stub", keyword="ruin").count(), 1)
