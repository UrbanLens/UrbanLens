"""The backfill that relabels connected-account imports filed as manual uploads.

What makes this worth testing rather than eyeballing: it matches on
``source_url`` prefixes, one of which is the user's own Immich server address.
Matching too widely would relabel a genuine upload; matching too narrowly leaves
a photo in the wrong Media gallery tab, which is the cost of being careful and
is the trade this takes.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.photos.google import media_item_web_url


class BackfillPersonalLibraryImageSourceTests(TestCase):
    """Only rows the two importers actually wrote are relabelled."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to bootstrap site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def _image(self, **kwargs) -> Image:
        defaults = {"profile": self.profile, "source": ImageSource.UPLOAD, "image": "pin_images/x.png"}
        return baker.make(Image, **{**defaults, **kwargs})

    def _run(self, *flags: str) -> str:
        out = StringIO()
        call_command("backfill_personal_library_image_source", *flags, stdout=out)
        return out.getvalue()

    def test_a_google_photos_import_is_relabelled(self) -> None:
        image = self._image(source_url=media_item_web_url("item-1"))

        self._run()

        image.refresh_from_db()
        self.assertEqual(image.source, ImageSource.GOOGLE_PHOTOS)

    def test_an_immich_import_is_relabelled_by_its_own_server(self) -> None:
        image = self._image(source_url=self.account.asset_web_url("asset-1"))

        self._run()

        image.refresh_from_db()
        self.assertEqual(image.source, ImageSource.IMMICH)

    def test_an_ordinary_upload_is_left_alone(self) -> None:
        """The failure that would matter: relabelling somebody's real upload."""
        plain = self._image(source_url="")
        pasted = self._image(source_url="https://example.com/a-photo-someone-linked.jpg")

        self._run()

        for image in (plain, pasted):
            image.refresh_from_db()
            with self.subTest(source_url=image.source_url):
                self.assertEqual(image.source, ImageSource.UPLOAD)

    def test_another_immich_server_is_not_matched(self) -> None:
        """Each account matches only its own host."""
        other = self._image(source_url="https://someone-elses-immich.example.net/photos/asset-9")

        self._run()

        other.refresh_from_db()
        self.assertEqual(other.source, ImageSource.UPLOAD)

    def test_a_row_already_labelled_is_not_touched(self) -> None:
        already = self._image(source=ImageSource.IMMICH, source_url=self.account.asset_web_url("asset-2"))

        output = self._run()

        already.refresh_from_db()
        self.assertEqual(already.source, ImageSource.IMMICH)
        self.assertIn("Nothing to relabel", output)

    def test_dry_run_reports_without_writing(self) -> None:
        image = self._image(source_url=media_item_web_url("item-2"))

        output = self._run("--dry-run")

        image.refresh_from_db()
        self.assertEqual(image.source, ImageSource.UPLOAD)
        self.assertIn("Would relabel 1", output)

    def test_the_url_shape_comes_from_the_code_that_writes_it(self) -> None:
        """Guard against the prefix being restated and drifting.

        The backfill recognises a row by the prefix of the URL its importer
        wrote. Two spellings of one format drift, and the copy is what drifts -
        so both sides come from the same function.
        """
        self.assertTrue(self.account.asset_web_url("asset-3").startswith(self.account.asset_url_prefix()))
