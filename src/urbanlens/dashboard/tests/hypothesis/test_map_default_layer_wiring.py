"""Every map opens on the base the viewer chose.

``Profile.default_map_view`` defaults to ``satellite``, but most maps were built without ever being
told what the viewer chose, and the shared layers engine substituted ``street`` - a value nobody
selected, and the metered vector base. These cover two pages whose maps the setting never reached:
an album's photo map, and the wiki page's annotations map.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class MapDefaultLayerTestCase(TestCase):
    """A signed-in viewer with a pin, which is what earns visibility of the pages below."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user of a fresh test db is promoted to bootstrap site admin
        self.user: User = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.location: Location = baker.make(Location, latitude="41.7", longitude="-73.9")
        self.pin: Pin = baker.make(Pin, profile=self.profile, location=self.location)

    def set_view(self, value: str) -> None:
        self.profile.default_map_view = value
        self.profile.save(update_fields=["default_map_view"])

    def rendered(self, url: str) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class AlbumMapDefaultLayerTests(MapDefaultLayerTestCase):
    """This map asked for ``remember`` against a site-wide storage key, so a viewer opening an album
    for the first time got the engine's own default whatever their profile said."""

    def setUp(self) -> None:
        super().setUp()
        self.album: Album = baker.make(Album, parent_pin=self.pin, profile=self.profile)
        image = baker.make(
            Image, pin=self.pin, profile=self.profile, latitude="41.7", longitude="-73.9", map_hidden=False
        )
        baker.make(AlbumItem, album=self.album, image=image)

    def _render(self) -> str:
        return self.rendered(
            reverse("pin.albums.detail", kwargs={"pin_slug": self.pin.slug, "album_slug": self.album.slug})
        )

    def test_the_album_map_is_told_the_viewers_configured_base(self) -> None:
        self.set_view("topographic")

        self.assertIn('data-default-base="topographic"', self._render())

    def test_an_unset_profile_gets_the_model_default_rather_than_street(self) -> None:
        self.assertEqual(self.profile.default_map_view, "satellite")

        body = self._render()

        self.assertIn('data-default-base="satellite"', body)
        self.assertNotIn('data-default-base="street"', body)

    def test_remember_reaches_the_client_verbatim(self) -> None:
        """Resolving this server-side would lose the per-browser restore: only the client knows what
        was stored, and whether it has anywhere to store it."""
        self.set_view("remember")

        self.assertIn('data-default-base="remember"', self._render())


class WikiAnnotationsMapDefaultLayerTests(MapDefaultLayerTestCase):
    """The wiki page's annotations map is the same entry the pin detail page uses.

    The pin page hands it ``data-default-map-view`` and the wiki page hands it nothing, so the
    viewer's setting was never consulted here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")

    def _render(self) -> str:
        return self.rendered(reverse("location.wiki", args=[self.location.slug]))

    def test_the_wiki_map_is_told_the_viewers_configured_base(self) -> None:
        """Carried by the layers panel this page already renders, so the page itself needs no key."""
        self.set_view("topographic")

        self.assertIn('data-default-base="topographic"', self._render())

    def test_the_wiki_map_names_no_base_of_its_own_to_override_it_with(self) -> None:
        """``map-annotations.ts`` passes this attribute straight through as the base, so any literal
        here - not just the one this page used to imply - would win over the panel's."""
        self.set_view("topographic")

        self.assertNotIn("data-default-map-view", self._render())
