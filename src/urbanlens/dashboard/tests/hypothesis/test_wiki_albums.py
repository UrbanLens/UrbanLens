"""Wiki-owned albums: the community half of the Album model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    pass


class WikiAlbumOwnershipTests(TestCase):
    """`parent_wiki` is a third owner kind, exclusive with the other two."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)

    def test_a_wiki_album_is_owned_by_its_wiki_alone(self) -> None:
        album = baker.make(Album, parent_wiki=self.wiki, name="Community Set", kind=AlbumKind.PLAIN)

        self.assertEqual(album.parent_wiki_id, self.wiki.pk)
        self.assertIsNone(album.parent_pin_id)
        self.assertIsNone(album.parent_profile_id)

    def test_for_wiki_returns_only_that_wikis_albums(self) -> None:
        mine = baker.make(Album, parent_wiki=self.wiki, name="Mine")
        other_wiki = baker.make(Wiki, location=baker.make(Location))
        baker.make(Album, parent_wiki=other_wiki, name="Theirs")

        self.assertEqual(list(Album.objects.for_wiki(self.wiki)), [mine])

    def test_a_pin_and_a_wiki_may_each_have_an_album_of_the_same_name(self) -> None:
        """The slug uniqueness constraint is per owner, and there are three owners."""
        pin = baker.make(Pin, profile=self.profile, location=self.location)
        pin_album = baker.make(Album, parent_pin=pin, name="Exteriors")
        wiki_album = baker.make(Album, parent_wiki=self.wiki, name="Exteriors")

        self.assertEqual(pin_album.slug, wiki_album.slug)
        self.assertNotEqual(pin_album.pk, wiki_album.pk)
