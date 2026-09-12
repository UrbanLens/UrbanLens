"""Wiki-owned albums: the community half of the Album model, and its concealment."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_CONCEALED = "urbanlens.dashboard.services.wiki.concealment.concealment_active"


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


class WikiAlbumConcealmentTests(TestCase):
    """A concealed viewer sees their own community albums and nobody else's."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile
        self.other = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        # A pin on the same location is what makes the wiki visible to the viewer.
        baker.make(Pin, profile=self.viewer, location=self.location)
        self.client.force_login(self.viewer_user)

        self.own = baker.make(Album, parent_wiki=self.wiki, name="My Contribution", profile=self.viewer)
        self.theirs = baker.make(Album, parent_wiki=self.wiki, name="Their Contribution", profile=self.other)
        self.url = reverse("location.wiki.albums", args=[self.location.slug])

    def _listing(self, *, concealed: bool) -> str:
        with mock.patch(_CONCEALED, return_value=concealed):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_unconcealed_viewer_sees_every_contributors_album(self) -> None:
        """Anti-vacuity, and the control for every assertion below.

        If this fails the fixture is wrong, and the concealed assertions would
        pass for the wrong reason - by nothing being listed at all.
        """
        body = self._listing(concealed=False)

        self.assertIn("My Contribution", body)
        self.assertIn("Their Contribution", body)

    def test_a_concealed_viewer_does_not_see_another_contributors_album(self) -> None:
        body = self._listing(concealed=True)

        self.assertNotIn("Their Contribution", body, "a concealed viewer must not be shown somebody else's album")

    def test_a_concealed_viewer_still_sees_their_own_album(self) -> None:
        """ "Your own work back is not a leak" - the rule `_resolve_album_owner` states."""
        body = self._listing(concealed=True)

        self.assertIn("My Contribution", body, "concealment must not hide the viewer's own contribution")


class WikiAlbumBySlugScopingTests(TestCase):
    """A by-slug lookup must be scoped to the viewer, not to the wiki.

    `concealment.visible_rows`' docstring records that nine call sites were once scoped to the wiki instead -
    "an existence oracle that answers 'is there a row N here' for rows concealment has already decided the
    account cannot see, and, on the mutating routes, lets it act on one"."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile
        self.other = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.viewer, location=self.location)
        self.client.force_login(self.viewer_user)
        self.theirs = baker.make(Album, parent_wiki=self.wiki, name="Their Contribution", profile=self.other)

    def _rename(self, *, concealed: bool):
        """POST a rename, which is the shape that matters.

        `AlbumEditView` is POST-only."""
        url = reverse("location.wiki.albums.edit", args=[self.location.slug, self.theirs.slug])
        with mock.patch(_CONCEALED, return_value=concealed):
            return self.client.post(url, {"name": "Renamed By Someone Else"})

    def test_a_concealed_viewer_cannot_rename_another_contributors_album(self) -> None:
        response = self._rename(concealed=True)

        self.assertEqual(response.status_code, 404, "the album must not resolve for a viewer who cannot see it")
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, "Their Contribution", "and nothing may have been written")

    def test_an_unconcealed_viewer_can(self) -> None:
        """Anti-vacuity: the 404 above must come from concealment, not from the route.

        Asserted as "not 404 and not 405" rather than a specific success code,
        so neither a method mismatch nor a missing route can satisfy it.
        """
        response = self._rename(concealed=False)

        self.assertNotIn(response.status_code, (404, 405), f"the route itself must work: got {response.status_code}")
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.name, "Renamed By Someone Else", "the rename must actually land")
