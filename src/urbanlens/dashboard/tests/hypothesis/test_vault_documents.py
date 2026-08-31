"""Tests for Vault > Documents (Batch 5 of the Vault feature): the
media_type=DOCUMENT gallery mirroring Vault Photos' grid/sort infrastructure,
and the .photos()/.documents() queryset split it depends on (see
models.images.queryset.ImageQuerySet and every call site that needed
narrowing from "everything this profile uploaded" to "photos only" once
documents became a real, reachable media type).
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.profile.model import Profile


class ImageQuerySetMediaSplitTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_photos_excludes_documents(self) -> None:
        photo = baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT)
        self.assertEqual(list(Image.objects.filter(profile=self.profile).photos()), [photo])

    def test_documents_excludes_photos(self) -> None:
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT)
        self.assertEqual(list(Image.objects.filter(profile=self.profile).documents()), [document])

    def test_photos_and_documents_both_exclude_video(self) -> None:
        # Neither is "not the other kind" - each must positively match its own
        # media_type, or a three-way split (PHOTO/VIDEO/DOCUMENT) with a bug
        # that inverts one filter would still pass a PHOTO-vs-DOCUMENT-only test.
        baker.make(Image, profile=self.profile, media_type=MediaKind.VIDEO)
        self.assertEqual(list(Image.objects.filter(profile=self.profile).photos()), [])
        self.assertEqual(list(Image.objects.filter(profile=self.profile).documents()), [])


class DocumentIconTests(TestCase):
    def test_pdf_gets_the_pdf_icon(self) -> None:
        image = baker.make(Image, media_type=MediaKind.DOCUMENT, caption="Deed.pdf")
        self.assertEqual(image.document_icon, "picture_as_pdf")

    def test_spreadsheet_gets_the_table_icon(self) -> None:
        image = baker.make(Image, media_type=MediaKind.DOCUMENT, caption="Budget.xlsx")
        self.assertEqual(image.document_icon, "table_chart")

    def test_unknown_extension_gets_the_generic_icon(self) -> None:
        image = baker.make(Image, media_type=MediaKind.DOCUMENT, caption="Notes.xyz")
        self.assertEqual(image.document_icon, "insert_drive_file")

    def test_extension_match_is_case_insensitive(self) -> None:
        image = baker.make(Image, media_type=MediaKind.DOCUMENT, caption="Deed.PDF")
        self.assertEqual(image.document_icon, "picture_as_pdf")

    def test_falls_back_to_the_stored_filename_when_there_is_no_caption(self) -> None:
        from urbanlens.core.tests.images import JPEG_BYTES

        image = baker.make(Image, media_type=MediaKind.DOCUMENT, caption="", image=SimpleUploadedFile("Report.pdf", JPEG_BYTES, content_type="application/pdf"))
        self.assertEqual(image.document_icon, "picture_as_pdf")


class VaultDocumentsViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_page_lists_only_documents(self) -> None:
        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="Permit.pdf")
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO, caption="not-a-document.jpg")
        response = self.client.get(reverse("vault.documents"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Permit.pdf")
        self.assertNotContains(response, "not-a-document.jpg")
        self.assertEqual(response.context["document_count"], 1)
        self.assertEqual(list(response.context["documents"]), [document])

    def test_upload_zone_hidden_without_the_feature(self) -> None:
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=False):
            response = self.client.get(reverse("vault.documents"))
        self.assertNotContains(response, 'id="documents-file-input"')
        self.assertContains(response, "aren't enabled for your account")


class DocumentItemsViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_returns_only_documents_paginated(self) -> None:
        for i in range(3):
            baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption=f"doc{i}.pdf")
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.documents.items"), {"offset": 0, "limit": 2})
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 2)

    def test_excludes_another_profiles_documents(self) -> None:
        other_profile = baker.make(User).profile
        baker.make(Image, profile=other_profile, media_type=MediaKind.DOCUMENT)
        response = self.client.get(reverse("vault.documents.items"))
        self.assertEqual(response.json()["total"], 0)

    def test_name_sort_orders_alphabetically(self) -> None:
        from urbanlens.dashboard.models.images.sort import GallerySort

        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="Zebra.pdf")
        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="Apple.pdf")
        response = self.client.get(reverse("vault.documents.items"), {"sort": GallerySort.NAME})
        captions = [item["caption"] for item in response.json()["items"]]
        self.assertEqual(captions, ["Apple.pdf", "Zebra.pdf"])

    def test_items_carry_the_server_resolved_document_icon(self) -> None:
        """The grid renders the icon from the payload rather than re-deriving it.

        The client only ever sees the caption, while the server falls back to
        the stored filename when a document has no caption - a client-side copy
        of the extension map therefore disagrees with the server-rendered first
        page for exactly those rows.
        """
        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="deed.pdf")
        item = self.client.get(reverse("vault.documents.items")).json()["items"][0]
        self.assertEqual(item["media_type"], MediaKind.DOCUMENT)
        self.assertEqual(item["document_icon"], "picture_as_pdf")

    def test_photo_items_carry_an_empty_document_icon(self) -> None:
        baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO, caption="not-a-doc.jpg")
        item = self.client.get(reverse("vault.photos.items")).json()["items"][0]
        self.assertEqual(item["media_type"], MediaKind.PHOTO)
        self.assertEqual(item["document_icon"], "")

    def test_query_count_does_not_grow_with_the_number_of_documents(self) -> None:
        """The gallery selects ``profile__user``, so listing is flat, not per-row.

        ``image_to_gallery_json`` names the uploader through
        ``Profile.username`` -> ``self.user.username``. Without that select
        each row costs a ``dashboard_profiles`` and an ``auth_user`` query, so
        a page of results scales with the page size - 2x the page size in extra
        round-trips on every infinite-scroll fetch.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def page_queries() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("vault.documents.items"))
            return len(ctx)

        for i in range(2):
            baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption=f"a{i}.pdf")
        with_two = page_queries()

        for i in range(6):
            baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption=f"b{i}.pdf")
        with_eight = page_queries()

        self.assertEqual(
            with_two,
            with_eight,
            f"listing 8 documents took {with_eight} queries vs {with_two} for 2 - the per-row uploader lookup is back",
        )


class DocumentUploadViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.url = reverse("vault.documents.upload")

    def _upload(self, name: str = "notes.txt", content: bytes = b"hello world", content_type: str = "text/plain"):
        return self.client.post(self.url, {"document": SimpleUploadedFile(name, content, content_type=content_type)})

    def test_missing_file_is_a_400(self) -> None:
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], "No document provided.")

    @patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
    def test_successful_upload_creates_a_document_row(self, mock_enqueue) -> None:
        response = self._upload()
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        image = Image.objects.get(profile=self.profile)
        self.assertEqual(image.media_type, MediaKind.DOCUMENT)
        self.assertEqual(image.caption, "notes.txt")
        mock_enqueue.assert_called_once()

    def test_document_upload_without_the_feature_is_403(self) -> None:
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=False):
            response = self._upload()
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)
        self.assertEqual(response.json()["error"], "Document uploads are not enabled for your account.")

    @patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
    def test_uploaded_document_does_not_appear_on_vault_photos(self, _mock_enqueue) -> None:
        self._upload()
        response = self.client.get(reverse("vault.photos"))
        self.assertEqual(response.context["photo_count"], 0)

    @patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
    def test_posting_an_image_to_the_document_endpoint_is_still_typed_as_a_photo(self, _mock_enqueue) -> None:
        """DocumentUploadView does no type-restriction of its own - it defers
        entirely to upload_photo()'s own content-based classification (see
        photo_upload._resolve_media_type). Posting a real image there
        correctly creates a PHOTO row (not a DOCUMENT one just because of
        which endpoint received it) - it just won't show up on this page,
        since VaultDocumentsView filters to .documents().
        """
        from urbanlens.core.tests.images import JPEG_BYTES

        response = self._upload(name="photo.jpg", content=JPEG_BYTES, content_type="image/jpeg")
        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        image = Image.objects.get(profile=self.profile)
        self.assertEqual(image.media_type, MediaKind.PHOTO)


class VaultPhotosExcludesDocumentsTests(TestCase):
    """Regression coverage for the .photos() fix in controllers.vault_photos -
    before it, a document uploaded via Vault Documents (or anywhere else)
    would silently appear in the Photos gallery, the organize queue, the home
    widget, and the external photos API.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, pin=None, wiki=None, caption="Deed.pdf")

    def test_vault_photos_gallery_excludes_it(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("vault.photos"))
        self.assertEqual(response.context["photo_count"], 0)
        self.assertNotIn(self.document, response.context["images"])

    def test_vault_photos_items_json_excludes_it(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("vault.photos.items"))
        self.assertEqual(response.json()["total"], 0)

    def test_organize_queue_excludes_it(self) -> None:
        # needs_attention() already requires pin/wiki/visit all unset, which
        # a fresh document satisfies - it would show up asking to be "filed"
        # like a geotagged photo, which makes no sense for a document.
        from urbanlens.dashboard.controllers.vault_photos import _attention_cards

        cards = _attention_cards(self.profile)
        self.assertEqual([c["image"] for c in cards], [])

    def test_home_widget_excludes_it(self) -> None:
        from urbanlens.dashboard.services.home.home_widgets import home_dashboard_context

        context = home_dashboard_context(self.profile)
        self.assertNotIn(self.document, list(context["home_recent_photos"]))

    def test_home_stats_photos_uploaded_count_excludes_it(self) -> None:
        from urbanlens.dashboard.services.home.home_widgets import home_dashboard_context

        context = home_dashboard_context(self.profile)
        photos_stat = next(s for s in context["home_stats"] if s["label"] == "Photos uploaded")
        self.assertEqual(photos_stat["value"], 0)

    def test_external_photos_api_still_includes_it(self) -> None:
        """The external "photos" API is a deliberately general media library -
        PhotoSerializer/build_photo_payload return media_type precisely so a
        client can tell a document/video apart from a photo, and POST already
        runs the same media-type-agnostic upload_photo(). Unlike every other
        surface in this test file, this one must NOT narrow to .photos() -
        confirming that directly, since it would be an easy, wrong "fix" to
        make by analogy with the others.
        """
        from urbanlens.dashboard.models.account.model import ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        api_key, raw_key = generate_api_key(self.user, "Test Key")
        api_key.scopes = [ApiKeyScope.PHOTOS_READ.value]
        api_key.save(update_fields=["scopes"])
        response = self.client.get(reverse("external_api:photos"), HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 200, response.content)
        uuids = [item["uuid"] for item in response.json()["results"]]
        self.assertIn(str(self.document.uuid), uuids)


class PhotoActionViewRefusesDocumentsTests(TestCase):
    """create-pin/log-visit/send-to-wiki are the three PhotoActionView actions
    that can give an Image a pin/wiki FK - each must refuse a document
    outright, since nothing downstream (the pin/wiki Photos gallery) can
    render one. delete/share/accept/reject/dismiss stay unrestricted (see the
    class docstring on PhotoActionView for why each is safe as-is).
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, pin=None, wiki=None)

    def _action_url(self, action: str) -> str:
        return reverse("vault.photos.action", args=[self.document.pk, action])

    def test_create_pin_refuses_a_document(self) -> None:
        response = self.client.post(self._action_url("create-pin"), {"latitude": "40.0", "longitude": "-74.0"})
        self.assertEqual(response.status_code, 200)
        self.document.refresh_from_db()
        self.assertIsNone(self.document.pin_id)

    def test_log_visit_refuses_a_document(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude=41.0, longitude=-73.0)
        pin = baker.make(Pin, profile=self.profile, location=location)
        response = self.client.post(self._action_url("log-visit"), {"pin_slug": pin.slug})
        self.assertEqual(response.status_code, 200)
        self.document.refresh_from_db()
        self.assertIsNone(self.document.pin_id)

    def test_send_to_wiki_refuses_a_document(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.wiki.model import Wiki

        location = baker.make(Location, latitude=42.0, longitude=-72.0)
        baker.make(Pin, profile=self.profile, location=location)
        wiki = baker.make(Wiki, location=location)
        response = self.client.post(self._action_url("send-to-wiki"), {"location_slug": location.slug})
        self.assertEqual(response.status_code, 200)
        self.document.refresh_from_db()
        self.assertIsNone(self.document.wiki_id)
        self.assertNotEqual(self.document.wiki_id, wiki.pk)


class CommentImageAttachExcludesDocumentsTests(TestCase):
    """attach_existing_comment_image (controllers.comments) must not let a
    document through - comment.image is always rendered as an <img>.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_a_document_is_not_attached(self) -> None:
        from urbanlens.dashboard.controllers.comments import attach_existing_comment_image
        from urbanlens.dashboard.models.comments.model import Comment

        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT)
        comment = baker.make(Comment, profile=self.profile)
        attach_existing_comment_image(comment, str(document.pk), self.profile)
        comment.refresh_from_db()
        self.assertFalse(comment.image)


class PinWikiGalleriesExcludeDocumentsTests(TestCase):
    """_pin_gallery_images/_wiki_gallery_images (controllers.image_gallery) -
    defense in depth alongside the PhotoActionView guards above, since these
    galleries have no document rendering at all.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_pin_gallery_excludes_a_document(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude=43.0, longitude=-71.0)
        pin = baker.make(Pin, profile=self.profile, location=location)
        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, pin=pin)
        response = self.client.get(reverse("pin.gallery.json", args=[pin.slug]))
        self.assertEqual(response.json()["images"], [])


class AchievementMetricExcludesDocumentsTests(TestCase):
    """The 'photos_uploaded' achievement metric (services.achievements.metrics)
    must count only photos, matching its own label.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_document_upload_does_not_count(self) -> None:
        from urbanlens.dashboard.models.images.model import ImageSource
        from urbanlens.dashboard.services.achievements.metrics import _photos_uploaded, _photos_uploaded_bulk

        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, source=ImageSource.UPLOAD)
        self.assertEqual(_photos_uploaded(self.profile), 0)
        self.assertEqual(_photos_uploaded_bulk([self.profile.pk]).get(self.profile.pk, 0), 0)


class MemoriesHeroStatsExcludeDocumentsTests(TestCase):
    """_compute_hero_stats' photo_count (controllers.memories) backs the
    Memories page hero, labeled "Photos".
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_document_does_not_count(self) -> None:
        from urbanlens.dashboard.controllers.memories import _compute_hero_stats

        baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT)
        hero_stats, _has_data = _compute_hero_stats(self.profile)
        self.assertEqual(hero_stats["photo_count"], 0)


class DocumentLightboxActionsTests(TestCase):
    """The lightbox must not offer the actions PhotoActionView refuses.

    File-to-a-pin and send-to-a-wiki are both refused server-side for a
    document (see PhotoActionViewRefusesDocumentsTests); rendering the buttons
    anyway leaves a user searching their pins, picking one, and getting an
    error toast for their trouble.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_associations_mark_a_document_as_unfileable(self) -> None:
        from urbanlens.dashboard.services.media.images import image_associations

        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="deed.pdf")
        self.assertFalse(image_associations(document, self.profile)["can_file"])

    def test_associations_mark_a_photo_as_fileable(self) -> None:
        from urbanlens.dashboard.services.media.images import image_associations

        photo = baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        self.assertTrue(image_associations(photo, self.profile)["can_file"])

    def test_document_associations_panel_offers_neither_action(self) -> None:
        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="deed.pdf")
        response = self.client.get(reverse("vault.photos.associations", args=[document.pk]))
        body = response.content.decode()
        self.assertNotIn("_lightboxOpenPinPicker", body)
        self.assertNotIn("_lightboxOpenWikiPicker", body)

    def test_photo_associations_panel_still_offers_both(self) -> None:
        photo = baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.associations", args=[photo.pk]))
        body = response.content.decode()
        self.assertIn("_lightboxOpenPinPicker", body)
        self.assertIn("_lightboxOpenWikiPicker", body)


class AlbumPickerExcludesDocumentsTests(TestCase):
    """Album tiles are <img> elements throughout, so a document in an album
    renders as a broken image and can even be chosen as the album cover.
    eligible_images_for is the chokepoint for both the picker listing and
    AlbumAddPhotosView, which re-scopes submitted ids through it.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_vault_album_candidates_exclude_documents(self) -> None:
        from urbanlens.dashboard.services.photos.albums import eligible_images_for

        photo = baker.make(Image, profile=self.profile, media_type=MediaKind.PHOTO)
        document = baker.make(Image, profile=self.profile, media_type=MediaKind.DOCUMENT, caption="deed.pdf")
        eligible = list(eligible_images_for(self.profile, self.profile))
        self.assertIn(photo, eligible)
        self.assertNotIn(document, eligible)
