"""The Photos tab's album grid pages, and the add-to-album picker reaches every album on its own (P171)."""

from __future__ import annotations

from http import HTTPStatus
import re
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_PAGE = 3
_PAGE_SIZE = "urbanlens.dashboard.controllers.albums.ALBUM_GRID_PAGE_SIZE"
_CONCEALED = "urbanlens.dashboard.services.wiki.concealment.concealment_active"
_NEXT = re.compile(r'class="album-target-more"[^>]*hx-get="([^"]+)"')
_PICKER_SLUG = re.compile(r'class="album-target-item"[^>]*data-slug="([^"]+)"')


def _albums(count: int, **owner) -> list[Album]:
    return [Album.objects.create(name=f"Album {index:02d}", **owner) for index in range(count)]


class _AlbumPagingTestCase(TestCase):
    def _grid_pages(self, url: str, **params) -> list[str]:
        """Every album slug the grid endpoint hands back, following offsets to the end."""
        slugs: list[str] = []
        offset = 0
        while True:
            response = self.client.get(url, {"albums": "1", "offset": offset, **params})
            self.assertEqual(response.status_code, HTTPStatus.OK)
            data = response.json()
            if not data["items"]:
                break
            slugs.extend(item["slug"] for item in data["items"])
            offset += len(data["items"])
        self.assertEqual(len(slugs), data["total"])
        return slugs

    def _picker_pages(self, url: str) -> tuple[list[str], str]:
        """Every album slug the picker rows offer, following its load-more rows to the end."""
        slugs: list[str] = []
        body = ""
        next_url: str | None = url
        pages = 0
        while next_url:
            response = self.client.get(next_url.replace("&amp;", "&"))
            self.assertEqual(response.status_code, HTTPStatus.OK)
            page = response.content.decode()
            body += page
            slugs.extend(_PICKER_SLUG.findall(page))
            match = _NEXT.search(page)
            next_url = match.group(1) if match else None
            pages += 1
            self.assertLess(pages, 50, "the picker's load-more never ends")
        return slugs, body


class PinAlbumGridPagingTests(_AlbumPagingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")
        self.albums = _albums(_PAGE * 2 + 1, profile=self.pin.profile, parent_pin=self.pin)
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums", args=[self.pin.slug])
        patcher = mock.patch(_PAGE_SIZE, _PAGE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_first_render_carries_one_page_and_the_true_count(self) -> None:
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(len(response.context["album_rows"]), _PAGE)
        self.assertEqual(response.context["album_count"], len(self.albums))
        body = response.content.decode()
        self.assertEqual(body.count('class="album-card"'), _PAGE)
        self.assertIn("data-album-grid", body)
        self.assertIn(f'data-photo-count="{len(self.albums)}"', body)

    def test_paging_returns_every_album_once_in_name_order(self) -> None:
        slugs = self._grid_pages(self.url)

        self.assertEqual(slugs, [album.slug for album in sorted(self.albums, key=lambda album: album.name)])

    def test_a_paged_card_is_the_same_card_the_first_page_renders(self) -> None:
        last = sorted(self.albums, key=lambda album: album.name)[-1]
        response = self.client.get(self.url, {"albums": "1", "offset": str(len(self.albums) - 1)})

        [item] = response.json()["items"]
        self.assertEqual(item["slug"], last.slug)
        self.assertIn('class="album-card"', item["html"])
        self.assertIn(reverse("pin.albums.detail", args=[self.pin.slug, last.slug]), item["html"])
        self.assertIn(reverse("pin.albums.delete", args=[self.pin.slug, last.slug]), item["html"])
        self.assertIn(reverse("pin.albums.add", args=[self.pin.slug, last.slug]), item["html"])

    def test_the_panel_does_not_embed_the_picker_rows(self) -> None:
        body = self.client.get(self.url).content.decode()

        self.assertIn("album-target-dialog", body)
        self.assertNotIn('class="album-target-item"', body)
        self.assertIn("data-picker-url", body)

    def test_the_picker_can_find_an_album_beyond_the_first_page(self) -> None:
        last = sorted(self.albums, key=lambda album: album.name)[-1]

        response = self.client.get(self.url, {"picker": "1", "q": last.name})

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(_PICKER_SLUG.findall(response.content.decode()), [last.slug])
        self.assertContains(response, reverse("pin.albums.add", args=[self.pin.slug, last.slug]))

    def test_the_picker_pages_to_every_album(self) -> None:
        slugs, _body = self._picker_pages(f"{self.url}?picker=1")

        self.assertEqual(sorted(slugs), sorted(album.slug for album in self.albums))
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_a_search_that_matches_nothing_says_so(self) -> None:
        response = self.client.get(self.url, {"picker": "1", "q": "no such album"})

        self.assertEqual(_PICKER_SLUG.findall(response.content.decode()), [])
        self.assertContains(response, "No albums match")

    def test_the_detail_view_picker_leaves_out_the_open_album(self) -> None:
        opened = self.albums[0]
        detail = self.client.get(reverse("pin.albums.detail", args=[self.pin.slug, opened.slug]))
        match = re.search(r'data-picker-url="([^"]+)"', detail.content.decode())
        assert match is not None
        picker_url = match.group(1)

        slugs, _body = self._picker_pages(picker_url.replace("&amp;", "&"))

        self.assertNotIn(opened.slug, slugs)
        self.assertEqual(len(slugs), len(self.albums) - 1)

    def test_an_owner_with_no_albums_is_told_to_create_one(self) -> None:
        Album.objects.filter(parent_pin=self.pin).delete()

        response = self.client.get(self.url, {"picker": "1"})

        self.assertContains(response, "Create an album first")


class ChildAlbumPagingTests(_AlbumPagingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = baker.make_recipe("dashboard.pin")
        self.child = baker.make_recipe("dashboard.detail_pin", profile=self.parent.profile, parent_pin=self.parent)
        self.child.slug = self.child.ensure_slug()
        self.child.save(update_fields=["slug"])
        self.parent_albums = _albums(_PAGE, profile=self.parent.profile, parent_pin=self.parent)
        self.child_album = Album.objects.create(
            name="Zz child album", profile=self.parent.profile, parent_pin=self.child
        )
        self.client.force_login(self.parent.profile.user)
        self.url = reverse("pin.albums", args=[self.parent.slug])
        patcher = mock.patch(_PAGE_SIZE, _PAGE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_include_children_pages_to_the_child_album(self) -> None:
        first = self.client.get(self.url, {"children": "1"})
        self.assertEqual(first.context["album_count"], _PAGE + 1)
        self.assertNotIn(self.child_album.name, [row["album"].name for row in first.context["album_rows"]])

        slugs = self._grid_pages(self.url, children="1")

        self.assertIn(self.child_album.slug, slugs)
        response = self.client.get(self.url, {"children": "1", "albums": "1", "offset": str(_PAGE)})
        [item] = response.json()["items"]
        self.assertIn(self.child.effective_name, item["html"], "a child album's card must still name its pin")
        self.assertIn(reverse("pin.albums.detail", args=[self.child.slug, self.child_album.slug]), item["html"])
        self.assertIn("from_pin=", item["html"])

    def test_without_children_the_child_album_is_not_listed(self) -> None:
        slugs = self._grid_pages(self.url)

        self.assertNotIn(self.child_album.slug, slugs)

    def test_the_children_picker_files_into_the_child_album_on_its_own_pin(self) -> None:
        response = self.client.get(self.url, {"children": "1", "picker": "1", "q": "child"})

        self.assertEqual(_PICKER_SLUG.findall(response.content.decode()), [self.child_album.slug])
        self.assertContains(response, reverse("pin.albums.add", args=[self.child.slug, self.child_album.slug]))


class VaultAlbumGridPagingTests(_AlbumPagingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.albums = _albums(_PAGE + 2, profile=self.profile, parent_profile=self.profile)
        self.client.force_login(self.user)
        self.url = reverse("vault.photos.albums")
        patcher = mock.patch(_PAGE_SIZE, _PAGE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_vault_album_is_reachable_through_the_grid_and_the_picker(self) -> None:
        self.assertEqual(len(self.client.get(self.url).context["album_rows"]), _PAGE)

        self.assertEqual(sorted(self._grid_pages(self.url)), sorted(album.slug for album in self.albums))
        picker_slugs, _body = self._picker_pages(f"{self.url}?picker=1")
        self.assertEqual(sorted(picker_slugs), sorted(album.slug for album in self.albums))


class WikiAlbumGridPagingTests(_AlbumPagingTestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile
        self.other = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.viewer, location=self.location)
        self.own = _albums(_PAGE + 1, profile=self.viewer, parent_wiki=self.wiki)
        self.theirs = Album.objects.create(name="Album 00 theirs", profile=self.other, parent_wiki=self.wiki)
        self.client.force_login(self.viewer_user)
        self.url = reverse("location.wiki.albums", args=[self.location.slug])
        patcher = mock.patch(_PAGE_SIZE, _PAGE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unconcealed_viewer_pages_to_every_album(self) -> None:
        with mock.patch(_CONCEALED, return_value=False):
            slugs = self._grid_pages(self.url)
            picker, _body = self._picker_pages(f"{self.url}?picker=1")

        expected = sorted(album.slug for album in [*self.own, self.theirs])
        self.assertEqual(sorted(slugs), expected)
        self.assertEqual(sorted(picker), expected)

    def test_a_concealed_viewer_is_not_paged_or_searched_to_another_contributors_album(self) -> None:
        with mock.patch(_CONCEALED, return_value=True):
            slugs = self._grid_pages(self.url)
            _picker, body = self._picker_pages(f"{self.url}?picker=1&q=theirs")
            count = self.client.get(self.url).context["album_count"]

        self.assertEqual(sorted(slugs), sorted(album.slug for album in self.own))
        self.assertNotIn(self.theirs.name, body)
        self.assertEqual(count, len(self.own))
