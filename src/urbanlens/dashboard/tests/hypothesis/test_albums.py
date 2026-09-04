"""Tests for photo Albums: model scoping, membership/ordering services, and the
external-media add path's implied relevance vote."""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import ALBUM_KIND_SPECS, Album, AlbumItem, AlbumKind, album_kind_spec
from urbanlens.dashboard.models.album.sort import ALBUM_SORT_SPECS, AlbumSort, album_sort_spec
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.services.photos.albums import (
    add_images_to_album,
    album_cover,
    album_images,
    albums_listing,
    albums_with_images,
    eligible_images_for,
    loose_images_for,
    remove_images_from_album,
    reorder_album_items,
)


def _pin_with_photos(count: int = 3):
    """A pin plus *count* photos attached to it, newest last."""
    pin = baker.make_recipe("dashboard.pin")
    images = [baker.make_recipe("dashboard.image", pin=pin, profile=pin.profile) for _ in range(count)]
    return pin, images


class AlbumSlugScopingTests(TestCase):
    """Album slugs are unique per owner, not globally."""

    def test_two_pins_may_each_have_an_album_of_the_same_name(self) -> None:
        pin_a = baker.make_recipe("dashboard.pin")
        pin_b = baker.make_recipe("dashboard.pin")
        album_a = Album.objects.create(name="Interior", profile=pin_a.profile, parent_pin=pin_a)
        album_b = Album.objects.create(name="Interior", profile=pin_b.profile, parent_pin=pin_b)
        self.assertEqual(album_a.slug, album_b.slug)

    def test_one_pin_cannot_reuse_a_slug(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        first = Album.objects.create(name="Interior", profile=pin.profile, parent_pin=pin)
        second = Album.objects.create(name="Interior", profile=pin.profile, parent_pin=pin)
        self.assertNotEqual(first.slug, second.slug)

    def test_defaults_to_a_plain_album(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        album = Album.objects.create(name="Interior", profile=pin.profile, parent_pin=pin)
        self.assertEqual(album.kind, AlbumKind.PLAIN)
        self.assertEqual(album.sort, AlbumSort.UPLOADED)


class AlbumScopeTests(TestCase):
    """An album may only hold photos belonging to its own owner."""

    def test_eligible_images_excludes_another_pins_photos(self) -> None:
        pin, images = _pin_with_photos(2)
        _other_pin, other_images = _pin_with_photos(1)

        eligible = eligible_images_for(pin, pin.profile)
        self.assertCountEqual([img.pk for img in eligible], [img.pk for img in images])
        self.assertNotIn(other_images[0].pk, [img.pk for img in eligible])


class AlbumMembershipTests(TestCase):
    """Adding, removing, and the loose-photo split."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(3)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)

    def test_add_images_files_them(self) -> None:
        added = add_images_to_album(self.album, self.images[:2], self.pin.profile)
        self.assertEqual(added, 2)
        self.assertEqual(AlbumItem.objects.for_album(self.album).count(), 2)

    def test_adding_the_same_photo_twice_is_a_no_op(self) -> None:
        add_images_to_album(self.album, [self.images[0]], self.pin.profile)
        added_again = add_images_to_album(self.album, [self.images[0]], self.pin.profile)
        self.assertEqual(added_again, 0)
        self.assertEqual(AlbumItem.objects.for_album(self.album).count(), 1)

    def test_a_photo_can_be_in_several_albums_at_once(self) -> None:
        other = Album.objects.create(name="Exterior", profile=self.pin.profile, parent_pin=self.pin)
        add_images_to_album(self.album, [self.images[0]], self.pin.profile)
        add_images_to_album(other, [self.images[0]], self.pin.profile)
        self.assertEqual(AlbumItem.objects.filter(image=self.images[0]).count(), 2)

    def test_loose_images_excludes_filed_photos(self) -> None:
        add_images_to_album(self.album, [self.images[0]], self.pin.profile)
        loose = loose_images_for(self.pin, self.pin.profile)
        self.assertNotIn(self.images[0].pk, [img.pk for img in loose])
        self.assertEqual(loose.count(), 2)

    def test_removing_keeps_the_photo_itself(self) -> None:
        add_images_to_album(self.album, [self.images[0]], self.pin.profile)
        removed = remove_images_from_album(self.album, [self.images[0].pk])
        self.assertEqual(removed, 1)
        self.assertTrue(Image.objects.filter(pk=self.images[0].pk).exists())

    def test_deleting_an_album_keeps_its_photos(self) -> None:
        add_images_to_album(self.album, self.images, self.pin.profile)
        self.album.delete()
        self.assertEqual(Image.objects.filter(pk__in=[img.pk for img in self.images]).count(), 3)

    def test_removing_the_cover_photo_clears_the_cover(self) -> None:
        add_images_to_album(self.album, self.images, self.pin.profile)
        self.album.cover_image = self.images[0]
        self.album.save(update_fields=["cover_image", "updated"])

        remove_images_from_album(self.album, [self.images[0].pk])
        self.album.refresh_from_db()
        self.assertIsNone(self.album.cover_image_id)

    def test_removing_a_photo_that_belongs_to_a_different_album_is_a_no_op(self) -> None:
        other = Album.objects.create(name="Exterior", profile=self.pin.profile, parent_pin=self.pin)
        add_images_to_album(other, [self.images[0]], self.pin.profile)

        removed = remove_images_from_album(self.album, [self.images[0].pk])

        self.assertEqual(removed, 0)
        self.assertTrue(AlbumItem.objects.filter(album=other, image=self.images[0]).exists())


class AlbumOrderingTests(TestCase):
    """Date/name sorts are live; custom order is only written on a drag."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(3)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)
        add_images_to_album(self.album, self.images, self.pin.profile)

    def _display_item_ids(self) -> list[int]:
        return list(AlbumItem.objects.in_display_order(self.album).values_list("pk", flat=True))

    def test_default_sort_is_newest_uploaded_and_leaves_order_null(self) -> None:
        self.assertEqual(self.album.sort, AlbumSort.UPLOADED)
        self.assertFalse(AlbumItem.objects.for_album(self.album).exclude(order=None).exists())
        ordered = album_images(self.album, self.pin.profile)
        self.assertEqual([img.pk for img in ordered], list(reversed([img.pk for img in self.images])))

    def test_reorder_stamps_every_item_and_switches_to_custom(self) -> None:
        reversed_ids = list(reversed(self._display_item_ids()))
        self.assertEqual(reorder_album_items(self.album, reversed_ids), 3)
        self.album.refresh_from_db()
        self.assertEqual(self.album.sort, AlbumSort.CUSTOM)
        self.assertEqual(self._display_item_ids(), reversed_ids)
        self.assertEqual(
            list(AlbumItem.objects.in_display_order(self.album).values_list("order", flat=True)), [0, 1, 2]
        )

    def test_reorder_ignores_ids_from_another_album(self) -> None:
        other = Album.objects.create(name="Exterior", profile=self.pin.profile, parent_pin=self.pin)
        add_images_to_album(other, [self.images[0]], self.pin.profile)
        foreign_id = AlbumItem.objects.for_album(other).first().pk

        ids = self._display_item_ids()
        self.assertEqual(reorder_album_items(self.album, [foreign_id, *ids]), 3)

    def test_reorder_with_no_recognized_ids_is_a_no_op(self) -> None:
        """An empty (or all-foreign) drop must not stamp orders or flip the sort."""
        reordered = reorder_album_items(self.album, [])
        self.assertEqual(reordered, 0)
        self.album.refresh_from_db()
        self.assertEqual(self.album.sort, AlbumSort.UPLOADED)
        self.assertFalse(AlbumItem.objects.for_album(self.album).exclude(order=None).exists())

    def test_reorder_splices_a_partial_window_into_the_full_album(self) -> None:
        extra = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        add_images_to_album(self.album, [extra], self.pin.profile)
        display = self._display_item_ids()
        window = [display[1], display[0], display[2]]
        reorder_album_items(self.album, window)
        self.assertEqual(self._display_item_ids(), [display[1], display[0], display[2], *display[3:]])

    def test_new_photos_follow_the_live_sort_without_rewriting_order(self) -> None:
        extra = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        add_images_to_album(self.album, [extra], self.pin.profile)
        self.assertIsNone(AlbumItem.objects.membership(self.album, extra).order)
        after = [img.pk for img in album_images(self.album, self.pin.profile)]
        self.assertEqual(after[0], extra.pk)
        self.assertEqual(after[1:], list(reversed([img.pk for img in self.images])))

    def test_after_a_custom_order_new_photos_appear_at_the_end(self) -> None:
        reorder_album_items(self.album, self._display_item_ids())
        before = [img.pk for img in album_images(self.album, self.pin.profile)]
        extra = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        add_images_to_album(self.album, [extra], self.pin.profile)
        after = [img.pk for img in album_images(self.album, self.pin.profile)]
        self.assertEqual(after, [*before, extra.pk])
        self.assertIsNone(AlbumItem.objects.membership(self.album, extra).order)

    def test_taken_sort_picks_up_metadata_edits_without_touching_order(self) -> None:
        from django.utils import timezone

        self.album.sort = AlbumSort.TAKEN
        self.album.save(update_fields=["sort"])
        oldest = self.images[0]
        oldest.taken_at = timezone.now()
        oldest.save(update_fields=["taken_at"])
        self.assertFalse(AlbumItem.objects.for_album(self.album).exclude(order=None).exists())
        ordered = album_images(self.album, self.pin.profile)
        self.assertEqual(ordered[0].pk, oldest.pk)

    def test_name_sort_picks_up_caption_edits_without_touching_order(self) -> None:
        self.album.sort = AlbumSort.NAME
        self.album.save(update_fields=["sort"])
        self.images[0].caption = "zeta"
        self.images[1].caption = "alpha"
        self.images[2].caption = "mu"
        for image in self.images:
            image.save(update_fields=["caption"])
        ordered = album_images(self.album, self.pin.profile)
        self.assertEqual([img.caption for img in ordered], ["alpha", "mu", "zeta"])
        self.assertFalse(AlbumItem.objects.for_album(self.album).exclude(order=None).exists())


class AlbumCoverTests(TestCase):
    """album_cover prefers the explicit cover, else the first item."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(2)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)

    def test_empty_album_has_no_cover(self) -> None:
        self.assertIsNone(album_cover(self.album, self.pin.profile))

    def test_falls_back_to_the_first_item(self) -> None:
        add_images_to_album(self.album, self.images, self.pin.profile)
        cover = album_cover(self.album, self.pin.profile)
        self.assertEqual(cover.pk, album_images(self.album, self.pin.profile)[0].pk)

    def test_explicit_cover_wins(self) -> None:
        add_images_to_album(self.album, self.images, self.pin.profile)
        ordered = album_images(self.album, self.pin.profile)
        self.album.cover_image = ordered[-1]
        self.assertEqual(album_cover(self.album, self.pin.profile).pk, ordered[-1].pk)


class AlbumVisibilityTests(TestCase):
    """Every read here must chain ``Image.visible_to`` - an album can't widen who sees a photo.

    A stranger profile shares no friendship, trip, or wiki with the pin's
    owner, so ``visible_to`` denies them regardless of the default visibility
    settings (see ``ImageQuerySet._shared_within_reach_of``: filing a photo
    under a pin is not itself sharing it). That makes "the owner sees the
    photos, a stranger sees none of them" a deterministic check that a future
    refactor didn't quietly drop the visibility filter from one of these
    helpers.
    """

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(2)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)
        add_images_to_album(self.album, self.images, self.pin.profile)
        self.album.cover_image = self.images[0]
        self.album.save(update_fields=["cover_image", "updated"])
        self.stranger = baker.make_recipe("dashboard.user").profile

    def test_album_images_hides_everything_from_a_stranger(self) -> None:
        self.assertEqual(len(album_images(self.album, self.pin.profile)), 2)
        self.assertEqual(album_images(self.album, self.stranger), [])

    def test_eligible_images_hides_everything_from_a_stranger(self) -> None:
        self.assertEqual(eligible_images_for(self.pin, self.pin.profile).count(), 2)
        self.assertEqual(eligible_images_for(self.pin, self.stranger).count(), 0)

    def test_loose_images_hides_everything_from_a_stranger(self) -> None:
        loose_pin, _images = _pin_with_photos(1)
        self.assertEqual(loose_images_for(loose_pin, loose_pin.profile).count(), 1)
        self.assertEqual(loose_images_for(loose_pin, self.stranger).count(), 0)

    def test_album_cover_hides_from_a_stranger(self) -> None:
        self.assertIsNotNone(album_cover(self.album, self.pin.profile))
        self.assertIsNone(album_cover(self.album, self.stranger))

    def test_albums_with_images_hides_from_a_stranger(self) -> None:
        [(_, owner_images)] = albums_with_images(self.pin, self.pin.profile)
        self.assertEqual(len(owner_images), 2)
        [(_, stranger_images)] = albums_with_images(self.pin, self.stranger)
        self.assertEqual(stranger_images, [])

    def test_albums_listing_hides_from_a_stranger(self) -> None:
        owner_entry = albums_listing(self.pin, self.pin.profile)[0]
        self.assertEqual(owner_entry.photo_count, 2)
        self.assertIsNotNone(owner_entry.cover)
        stranger_entry = albums_listing(self.pin, self.stranger)[0]
        self.assertEqual(stranger_entry.photo_count, 0)
        self.assertIsNone(stranger_entry.cover)


class AlbumKindSpecTests(TestCase):
    """Kind behaviour is data-driven, so adding a kind is a one-place change."""

    def test_every_kind_has_a_spec(self) -> None:
        for value in AlbumKind.values:
            self.assertIn(value, ALBUM_KIND_SPECS)

    def test_unknown_kind_falls_back_to_plain(self) -> None:
        self.assertEqual(album_kind_spec("not-a-kind").kind, AlbumKind.PLAIN)

    def test_plain_albums_carry_no_badge(self) -> None:
        self.assertFalse(album_kind_spec(AlbumKind.PLAIN).badge)

    def test_every_sort_has_a_spec(self) -> None:
        for value in AlbumSort.values:
            self.assertIn(value, ALBUM_SORT_SPECS)

    def test_unknown_sort_falls_back_to_uploaded(self) -> None:
        self.assertEqual(album_sort_spec("not-a-sort").sort, AlbumSort.UPLOADED)

    def test_timelapse_defaults_to_date_taken(self) -> None:
        self.assertEqual(album_kind_spec(AlbumKind.TIMELAPSE).default_sort, AlbumSort.TAKEN)
        self.assertEqual(album_kind_spec(AlbumKind.PLAIN).default_sort, AlbumSort.UPLOADED)

    def test_album_exposes_its_own_spec(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        album = Album.objects.create(name="Series", kind=AlbumKind.TIMELAPSE, profile=pin.profile, parent_pin=pin)
        self.assertEqual(album.spec.icon, "timelapse")


class AlbumBatchingTests(TestCase):
    """albums_with_images resolves every album in a fixed number of queries."""

    def test_query_count_does_not_grow_with_album_count(self) -> None:
        """The invariant is constancy, not a specific number.

        ``visible_to`` issues several queries of its own to build the viewer's
        allowed-uploader set, and that's free to change - what must not change
        is that resolving eight albums costs the same as resolving two.

        """
        pin, images = _pin_with_photos(4)
        for index in range(2):
            album = Album.objects.create(name=f"A{index}", profile=pin.profile, parent_pin=pin)
            add_images_to_album(album, images, pin.profile)

        with CaptureQueriesContext(connection) as two_albums:
            self.assertEqual(len(albums_with_images(pin, pin.profile)), 2)

        for index in range(2, 8):
            album = Album.objects.create(name=f"A{index}", profile=pin.profile, parent_pin=pin)
            add_images_to_album(album, images, pin.profile)

        with CaptureQueriesContext(connection) as eight_albums:
            self.assertEqual(len(albums_with_images(pin, pin.profile)), 8)

        self.assertEqual(len(eight_albums), len(two_albums))

    def test_batched_result_matches_the_single_album_path(self) -> None:
        pin, images = _pin_with_photos(3)
        album = Album.objects.create(name="Interior", profile=pin.profile, parent_pin=pin)
        add_images_to_album(album, images, pin.profile)
        reorder_album_items(
            album, list(reversed(list(AlbumItem.objects.in_display_order(album).values_list("pk", flat=True))))
        )
        album.refresh_from_db()

        batched = {a.pk: imgs for a, imgs in albums_with_images(pin, pin.profile)}
        self.assertEqual(
            [img.pk for img in batched[album.pk]],
            [img.pk for img in album_images(album, pin.profile)],
        )

    def test_empty_owner_short_circuits_to_one_query(self) -> None:
        """No albums means no membership or visibility work at all."""
        pin = baker.make_recipe("dashboard.pin")
        with self.assertNumQueries(1):
            self.assertEqual(albums_with_images(pin, pin.profile), [])


class AlbumListingTests(TestCase):
    """The Photos tab listing does not hydrate every member photo."""

    def test_listing_matches_albums_with_images_for_cover_and_count(self) -> None:
        pin, images = _pin_with_photos(3)
        album = Album.objects.create(name="Interior", profile=pin.profile, parent_pin=pin)
        add_images_to_album(album, images, pin.profile)

        listing = {entry.album.pk: entry for entry in albums_listing(pin, pin.profile)}
        full = {album.pk: imgs for album, imgs in albums_with_images(pin, pin.profile)}

        self.assertEqual(listing[album.pk].photo_count, len(full[album.pk]))
        self.assertEqual(listing[album.pk].cover.pk, full[album.pk][0].pk)

    def test_listing_query_count_does_not_grow_with_album_count(self) -> None:
        pin, images = _pin_with_photos(4)
        for index in range(2):
            album = Album.objects.create(name=f"A{index}", profile=pin.profile, parent_pin=pin)
            add_images_to_album(album, images, pin.profile)

        with CaptureQueriesContext(connection) as two_albums:
            self.assertEqual(len(albums_listing(pin, pin.profile)), 2)

        for index in range(2, 8):
            album = Album.objects.create(name=f"A{index}", profile=pin.profile, parent_pin=pin)
            add_images_to_album(album, images, pin.profile)

        with CaptureQueriesContext(connection) as eight_albums:
            self.assertEqual(len(albums_listing(pin, pin.profile)), 8)

        self.assertEqual(len(eight_albums), len(two_albums))


class CacheMediaItemIntoAlbumTaskTests(TestCase):
    """The Celery task owns only the download half; the vote is already written."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)

    def _run(self):
        from urbanlens.dashboard.tasks import cache_media_item_into_album

        return cache_media_item_into_album(
            self.album.pk, self.pin.profile.pk, "wikimedia", "https://example.test/p.jpg"
        )

    def test_files_the_materialized_image_into_the_album(self) -> None:
        fake_image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        with (
            mock.patch(
                "urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=fake_image
            ),
            mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote,
        ):
            result = self._run()

        self.assertEqual(result, fake_image.pk)
        self.assertTrue(AlbumItem.objects.filter(album=self.album, image=fake_image).exists())
        queue_vote.assert_called_once()

    def test_a_failed_download_returns_none_without_raising(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError

        with mock.patch(
            "urbanlens.dashboard.services.media.media_materialize.materialize_media_item",
            side_effect=MaterializeError("boom"),
        ):
            self.assertIsNone(self._run())
        self.assertEqual(AlbumItem.objects.for_album(self.album).count(), 0)

    def test_a_deleted_album_is_a_no_op(self) -> None:
        album_id = self.album.pk
        self.album.delete()
        from urbanlens.dashboard.tasks import cache_media_item_into_album

        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as materialize:
            self.assertIsNone(
                cache_media_item_into_album(album_id, self.pin.profile.pk, "wikimedia", "https://example.test/p.jpg")
            )
        materialize.assert_not_called()


class ExternalMediaAlbumAddTests(TestCase):
    """Adding an external media item to an album implies a 'relevant' vote and caches it."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")
        self.location = self.pin.location
        self.profile = self.pin.profile
        self.url = "https://example.test/photo.jpg"
        self.source = "wikimedia"

    def _call(self, **overrides):
        from urbanlens.dashboard.services.media.media_relevance import VotePolicy, record_relevant_and_cache

        kwargs = {
            "location": self.location,
            "profile": self.profile,
            "source": self.source,
            "url": self.url,
            "pin": self.pin,
            "policy": VotePolicy.IMPLIED,
        }
        kwargs.update(overrides)
        return record_relevant_and_cache(**kwargs)

    def test_records_a_relevant_vote_and_caches(self) -> None:
        fake_image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.profile)
        with (
            mock.patch(
                "urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=fake_image
            ) as materialize,
            mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote,
        ):
            result = self._call()

        self.assertTrue(result.voted)
        self.assertFalse(result.declined)
        self.assertEqual(result.image, fake_image)
        materialize.assert_called_once()
        queue_vote.assert_called_once()

        vote = MediaRelevance.objects.get(
            profile=self.profile, location=self.location, source=self.source, item_key=media_item_key(self.url)
        )
        self.assertTrue(vote.is_relevant)

    def test_respects_an_existing_not_relevant_vote(self) -> None:
        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.location,
            source=self.source,
            item_key=media_item_key(self.url),
            is_relevant=False,
        )
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as materialize:
            result = self._call()

        self.assertTrue(result.declined)
        self.assertFalse(result.voted)
        self.assertIsNone(result.image)
        materialize.assert_not_called()
        # The user's deliberate down-vote must survive untouched.
        vote = MediaRelevance.objects.get(
            profile=self.profile, location=self.location, source=self.source, item_key=media_item_key(self.url)
        )
        self.assertFalse(vote.is_relevant)

    def test_an_existing_relevant_vote_still_caches(self) -> None:
        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.location,
            source=self.source,
            item_key=media_item_key(self.url),
            is_relevant=True,
        )
        fake_image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.profile)
        with (
            mock.patch(
                "urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=fake_image
            ),
            mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote"),
        ):
            result = self._call()

        self.assertFalse(result.declined)
        self.assertEqual(result.image, fake_image)

    def test_a_failed_download_keeps_the_vote(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError

        with mock.patch(
            "urbanlens.dashboard.services.media.media_materialize.materialize_media_item",
            side_effect=MaterializeError("boom"),
        ):
            result = self._call()

        self.assertTrue(result.voted)
        self.assertIsNone(result.image)
        self.assertIsNotNone(result.error)
        # The raw exception text must not reach the user.
        self.assertNotIn("boom", result.error)
        self.assertTrue(
            MediaRelevance.objects.get(
                profile=self.profile, location=self.location, source=self.source, item_key=media_item_key(self.url)
            ).is_relevant
        )

    def test_explicit_policy_overrides_a_prior_downvote(self) -> None:
        """An explicit thumbs-up is deliberate, so it replaces an earlier down-vote."""
        from urbanlens.dashboard.services.media.media_relevance import VotePolicy

        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.location,
            source=self.source,
            item_key=media_item_key(self.url),
            is_relevant=False,
        )
        fake_image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.profile)
        with (
            mock.patch(
                "urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=fake_image
            ),
            mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote"),
        ):
            result = self._call(policy=VotePolicy.EXPLICIT)

        self.assertFalse(result.declined)
        self.assertTrue(result.voted)
        vote = MediaRelevance.objects.get(
            profile=self.profile, location=self.location, source=self.source, item_key=media_item_key(self.url)
        )
        self.assertTrue(vote.is_relevant)

    def test_materialize_false_records_the_vote_without_downloading(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as materialize:
            result = self._call(materialize=False)

        self.assertTrue(result.voted)
        self.assertIsNone(result.image)
        self.assertIsNone(result.error)
        materialize.assert_not_called()
        self.assertTrue(
            MediaRelevance.objects.get(
                profile=self.profile, location=self.location, source=self.source, item_key=media_item_key(self.url)
            ).is_relevant
        )
