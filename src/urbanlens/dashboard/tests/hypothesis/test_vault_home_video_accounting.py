"""The Vault home's arithmetic has to cover every media kind it is billed for.

``MediaKind`` has three values; the Vault home counted two. A video's bytes are
already on the storage bar - ``get_storage_totals`` aggregates every row of the
profile with no media-type filter - so the number was never wrong. What was
wrong is that nothing on the page accounted for it: three tiles and a recent
strip explaining a bar bigger than all of them, and an ``is_empty`` that told a
user holding nothing but videos their Vault was empty while charging them for
it.

Two things worth knowing before changing this:

- **A video has no thumbnail and never will.** Thumbnails are written only in
  the photo branch of upload processing, and the hourly backfill filters to
  ``media_type=PHOTO``, so ``thumb_url`` falls through to the file itself.
  Rendering one through the photo tile downloads the whole video to display a
  broken image, which is why the recent strip branches on *photo* rather than
  on *document*.
- **The delete already worked.** ``PhotoActionView`` enforces ownership and
  deliberately does not restrict media type; nothing had ever handed a user one
  of their videos' ids. The missing piece was markup, not an endpoint.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind, QuotaExemption
from urbanlens.dashboard.services.media.storage import get_storage_totals, get_storage_used_bytes


class MediaKindCoverageTests(SimpleTestCase):
    """A fourth kind must not be able to repeat this quietly."""

    def test_every_media_kind_is_one_the_vault_home_counts(self) -> None:
        self.assertEqual(
            {MediaKind.PHOTO, MediaKind.VIDEO, MediaKind.DOCUMENT},
            set(MediaKind.values),
            "A media kind the Vault home does not count is charged to its owner's quota and shown nowhere.",
        )


class VaultHomeVideoAccountingTests(TestCase):
    """What the Vault home says about videos it is already billing for."""

    def setUp(self) -> None:
        super().setUp()
        # The first user in a fresh database is promoted to site admin, which
        # would hand this account every feature and change what it can reach.
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.url = reverse("vault.home")

    def _video(self, *, size: int = 1000, profile=None, **kwargs) -> Image:
        """A stored video row.

        Baked rather than uploaded: `upload_photo` needs the VIDEO_UPLOADS
        entitlement and ffmpeg, neither of which is what these tests are about.
        """
        return baker.make(Image, profile=profile or self.profile, media_type=MediaKind.VIDEO, file_size=size, **kwargs)

    def test_a_video_only_vault_is_not_reported_as_empty(self) -> None:
        """The headline: the page told a billed user they had nothing."""
        self._video()

        response = self.client.get(self.url)

        self.assertFalse(response.context["is_empty"])
        self.assertNotContains(response, "Your Vault is empty")

    def test_the_video_count_covers_only_this_profiles_videos(self) -> None:
        self._video()
        self._video()
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT)
        self._video(profile=baker.make(User).profile)

        context = self.client.get(self.url).context

        self.assertEqual((context["video_count"], context["photo_count"], context["document_count"]), (2, 1, 1))

    def test_the_video_bytes_are_a_real_slice_of_the_bar_they_explain(self) -> None:
        self._video(size=5_000_000)
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO, file_size=1_000)

        context = self.client.get(self.url).context

        self.assertEqual(context["storage_video_bytes"], 5_000_000)
        self.assertEqual(context["storage_used_bytes"], get_storage_totals(self.profile)[0])
        self.assertLessEqual(context["storage_video_bytes"], context["storage_used_bytes"])

    def test_exempt_video_bytes_are_not_added_to_the_counted_bar(self) -> None:
        """Split on the same predicate `get_storage_totals` splits on, or the
        explanation can exceed the thing it explains."""
        self._video(size=5_000_000, quota_exempt_reason=QuotaExemption.SHARED_COPY)

        context = self.client.get(self.url).context

        self.assertEqual(context["video_count"], 1)
        self.assertEqual(context["storage_video_bytes"], 0)

    def test_the_recent_strip_includes_a_video(self) -> None:
        video = self._video()

        context = self.client.get(self.url).context

        self.assertIn(video.pk, [item.pk for item in context["recent_uploads"]])

    def test_a_video_is_not_rendered_through_the_photo_tile(self) -> None:
        """`thumb_url` falls through to the file, so an `<img>` would fetch the whole video."""
        video = self._video(image=SimpleUploadedFile("clip.mp4", b"\x00\x00", content_type="video/mp4"))

        content = self.client.get(self.url).content.decode()

        self.assertNotIn(f'<img src="{video.image.url}"', content)
        self.assertIn("videocam", content)

    def test_the_videos_section_offers_a_delete_that_targets_the_row(self) -> None:
        video = self._video()

        content = self.client.get(self.url).content.decode()

        self.assertIn(reverse("vault.photos.action", args=[video.pk, "delete"]), content)
        self.assertIn(f'id="vault-video-{video.pk}"', content)

    def test_deleting_a_video_removes_the_row_and_frees_the_quota(self) -> None:
        """The affordance the new markup points at has to work for a video too."""
        video = self._video(size=4_000, image="")
        self.assertEqual(get_storage_used_bytes(self.profile), 4_000)

        response = self.client.post(reverse("vault.photos.action", args=[video.pk, "delete"]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Image.objects.filter(pk=video.pk).exists())
        self.assertEqual(get_storage_used_bytes(self.profile), 0)

    def test_a_second_profiles_video_cannot_be_deleted(self) -> None:
        video = self._video(profile=baker.make(User).profile, image="")

        response = self.client.post(reverse("vault.photos.action", args=[video.pk, "delete"]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Image.objects.filter(pk=video.pk).exists())
