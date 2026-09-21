"""Who can fetch a basemap tile, now that the answer is cached rather than asked every time.

``BasemapTileView`` remembers, against the session key, that a session was signed in - so a
viewport costs one such check instead of thirty (``P125``). That is a gate, so the interesting
tests are the ones that try to get a tile without being entitled to one, and the ones that check
the remembered answer cannot be spent on anything else.

``TILE_AUTH_TTL`` is the window in which a revocation that does not pass through logout - an
account disabled by an admin, a password change invalidating other sessions - still draws tiles.
That is the accepted trade; what is tested here is that the window is bounded, that it applies to
tiles alone, and that signing out does not wait for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import basemap_tiles
from urbanlens.dashboard.services.map.basemap_catalogue import CATALOGUE_CACHE_KEY
from urbanlens.dashboard.services.map.tile_authorisation import TILE_AUTH_TTL, tile_auth_key

if TYPE_CHECKING:
    from django.test import Client

_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

TILE_BYTES = b"x" * 128


class TileAuthorisationTests(TestCase):
    """The gate, probed from the outside."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        basemap_tiles.UpstreamSlots.reset()
        self.addCleanup(basemap_tiles.UpstreamSlots.reset)
        cache.set("ul_basemap_tile_street_12_1204_1539", (TILE_BYTES, "image/png"), 60)

    @property
    def url(self) -> str:
        return reverse("map.basemap_tiles", kwargs={"layer": "street", "z": 12, "x": 1204, "y": 1539})

    def _fetch(self, client: Client) -> int:
        with mock.patch(_CONFIGURED, return_value=True):
            return client.get(self.url).status_code

    def test_a_signed_in_viewer_gets_the_tile(self) -> None:
        """The positive control: everything below is only meaningful if this passes."""
        self.client.force_login(self.user)

        self.assertEqual(self._fetch(self.client), 200)

    def test_a_visitor_with_no_session_at_all_gets_no_tile(self) -> None:
        """No session key, so nothing to look up and nothing to fall back on but the real check."""
        self.assertNotEqual(self._fetch(self.client), 200)

    def test_a_session_that_never_signed_in_gets_no_tile(self) -> None:
        """Having a session is not the same as having signed in, and the key alone must not pass."""
        self.client.get("/")  # gives this client a session without authenticating it
        self.assertNotEqual(self._fetch(self.client), 200)

    def test_one_session_cannot_spend_another_session_s_answer(self) -> None:
        """The entry is keyed by session, so a second browser must establish its own."""
        self.client.force_login(self.user)
        self.assertEqual(self._fetch(self.client), 200)

        stranger = self.client_class()
        stranger.get("/")

        self.assertNotEqual(self._fetch(stranger), 200)

    def test_signing_out_revokes_the_tile_immediately(self) -> None:
        """Logout must not leave the session drawing tiles for the rest of the TTL."""
        self.client.force_login(self.user)
        self.assertEqual(self._fetch(self.client), 200)
        session_key = self.client.session.session_key
        self.assertIsNotNone(session_key)

        self.client.logout()

        self.assertIsNone(cache.get(tile_auth_key(str(session_key))))
        self.assertNotEqual(self._fetch(self.client), 200)

    def test_the_remembered_answer_expires(self) -> None:
        """The revocation window is whatever this timeout is, so it is written down and asserted."""
        self.client.force_login(self.user)

        store = caches[settings.PROXIED_BYTES_CACHE]
        with mock.patch.object(store, "set", wraps=store.set) as writes:
            self._fetch(self.client)

        timeouts = [
            call.kwargs.get("timeout", call.args[2] if len(call.args) > 2 else None)
            for call in writes.call_args_list
            if str(call.args[0]).startswith("ul_tileauth_")
        ]
        self.assertEqual(timeouts, [TILE_AUTH_TTL])
        self.assertLessEqual(
            TILE_AUTH_TTL, 3600, "a revocation window longer than an hour is not what this was agreed at"
        )

    def test_the_answer_buys_tiles_and_nothing_else(self) -> None:
        """A cached 'yes' must not become a general-purpose session.

        The catalogue sits on the same route prefix, is the nearest thing to a lookalike, and is
        the one endpoint a viewer holding tile access might plausibly be handed by mistake.
        """
        # Seeded so the catalogue answers from cache: REData is stubbed everywhere in these tests,
        # and this one is about who gets an answer, not where it came from.
        cache.set(
            CATALOGUE_CACHE_KEY,
            [
                {
                    "id": "street",
                    "name": "street",
                    "source_type": "raster",
                    "attribution": "x",
                    "url_template": "/t/{z}/{x}/{y}/",
                }
            ],
            60,
        )
        self.client.force_login(self.user)
        self.assertEqual(self._fetch(self.client), 200)

        with mock.patch(_CONFIGURED, return_value=True):
            signed_in = self.client.get(reverse("map.basemap_tiles.sources"))
        self.assertEqual(signed_in.status_code, 200, "the catalogue is expected to work while signed in")

        self.client.logout()

        with mock.patch(_CONFIGURED, return_value=True):
            signed_out = self.client.get(reverse("map.basemap_tiles.sources"))
        self.assertNotEqual(signed_out.status_code, 200)

    def test_a_restored_entry_on_a_dead_session_still_gets_nothing(self) -> None:
        """The strongest version of the attack: the cookie, and the entry, after the session is gone.

        The remembered answer is only half the gate - the session itself has to still name someone
        - so putting the entry back by hand must not be enough. Worth pinning rather than assuming:
        the first version of this relied on a middleware happening to load the session first, which
        would have made it true by accident and ordering-dependent.
        """
        self.client.force_login(self.user)
        self.assertEqual(self._fetch(self.client), 200)
        stolen = self.client.cookies.copy()
        remembered = tile_auth_key(str(self.client.session.session_key))

        self.client.logout()
        cache.set(remembered, True, timeout=TILE_AUTH_TTL)

        attacker = self.client_class()
        attacker.cookies.update(stolen)

        self.assertNotEqual(self._fetch(attacker), 200)

    def test_a_tile_on_a_remembered_session_asks_the_database_for_nothing(self) -> None:
        """The reason any of this exists, asserted where a regression would land."""
        self.client.force_login(self.user)
        self._fetch(self.client)

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self._fetch(self.client), 200)

        self.assertEqual([q["sql"] for q in queries.captured_queries], [])
