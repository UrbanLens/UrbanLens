"""What a model wrote, where a building or a cached lookup is, which map tile someone looked at, and whose address or
login it was stay out of log lines.

Pin names name undisclosed sites and coordinates locate them (``services.security.redact``); a trivia question is
generated from a wiki's text, a lock key can carry the point it locks, a high-zoom tile index is a building, and a
lockout counter is keyed by whatever a visitor typed.
"""

from __future__ import annotations

from contextlib import ExitStack
import logging
import smtplib
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.log_output import handler_output
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.account import _is_locked_out, _lockout_key_for_identifier
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway
from urbanlens.dashboard.services.core import coalesce, counters, locks
from urbanlens.dashboard.services.notifications.notification_delivery import send_email_now
from urbanlens.dashboard.services.pins.pin_restructure import building_footprint
from urbanlens.dashboard.services.pins.pin_subresources import (
    PinSubResourceError,
    create_pin_alias,
    create_pin_link,
    delete_pin_alias,
)
from urbanlens.dashboard.services.security.redact import redact_cache_key, redact_tile
from urbanlens.dashboard.services.trivia.classifier import ClassifierVerdict, classify_trivia_question
from urbanlens.dashboard.services.trivia.generation import generate_questions_for_wiki
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_MARKER = "Quokkabridge Sanatorium"
_LATITUDE, _LONGITUDE = "41.71825", "-73.93402"
_POINT_KEY = f"redata:context:{_LATITUDE},{_LONGITUDE}"


def _text(records: list[logging.LogRecord]) -> str:
    """Every captured record as it would be written, traceback included."""
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


class _Gateway:
    model = "fake-model"

    def __init__(self, answer: str | None = None, pairs: list[str] | None = None) -> None:
        self._answer, self._pairs = answer, pairs or []

    def send_prompt(self, prompt: str, **kwargs: object) -> str | None:  # noqa: ARG002 - the gateway's signature
        return self._answer

    def send_prompt_list(self, prompt: str, **kwargs: object) -> list[str]:  # noqa: ARG002
        return self._pairs

    @property
    def cost(self) -> int:
        return 0


class WhatATriviaModelWroteTests(TestCase):
    """A generated question quotes the wiki it was written from; a classifier's stray reply can quote anything."""

    def test_an_unrecognised_classifier_reply_is_not_logged(self) -> None:
        location = baker.make(Location, official_name="Old Armory")
        with (
            mock.patch(
                "urbanlens.dashboard.services.trivia.classifier.get_gateway",
                return_value=_Gateway(answer=f"{_MARKER} looks fine to me"),
            ),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            verdict = classify_trivia_question("What year was it built?", "1937", location)

        self.assertFalse(verdict.approved)
        self.assertNotIn("quokkabridge", _text(captured.records).casefold())

    def _generate(self, pairs: list[str], verdict: ClassifierVerdict) -> str:
        wiki = baker.make(
            Wiki, location=baker.make(Location), description="This building has a long and storied history. " * 20
        )
        with (
            mock.patch(
                "urbanlens.dashboard.services.trivia.generation.get_gateway", return_value=_Gateway(pairs=pairs)
            ),
            mock.patch("urbanlens.dashboard.services.trivia.generation.classify_trivia_question", return_value=verdict),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            generate_questions_for_wiki(wiki)
        return _text(captured.records)

    def test_a_pair_with_no_separator_is_not_logged(self) -> None:
        logged = self._generate([f"When did {_MARKER} close"], ClassifierVerdict(approved=True))

        self.assertIn("separator", logged, "the discarded pair was not logged at all")
        self.assertNotIn("Quokkabridge", logged)

    def test_a_rejected_question_is_not_logged(self) -> None:
        logged = self._generate(
            [f"When did {_MARKER} close?|||1994"], ClassifierVerdict(approved=False, reason="person")
        )

        self.assertIn("person", logged, "the rejection was not logged with its reason")
        self.assertNotIn("Quokkabridge", logged)


class AnUnparseableBuildingFootprintTests(SimpleTestCase):
    def test_its_coordinates_are_not_logged(self) -> None:
        geometry = {"type": "Polygon", "coordinates": [[[float(_LONGITUDE), float(_LATITUDE)], [-73.9, 41.7]]]}

        with self.assertLogs("urbanlens", level="DEBUG") as captured:
            self.assertIsNone(building_footprint({"geometry": geometry}))

        logged = _text(captured.records)
        self.assertIn("Polygon", logged, "the failure was not logged with the geometry's type")
        self.assertNotIn("41.718", logged)
        self.assertNotIn("73.934", logged)


class ACacheKeyThatLocatesAPlaceTests(SimpleTestCase):
    """``redata_point_data`` keys its shared lookups by the point: ``redata:context:<lat>,<lng>``."""

    def test_a_failed_share_does_not_log_the_point(self) -> None:
        with (
            mock.patch.object(coalesce.cache, "get", return_value=None),
            mock.patch.object(coalesce, "acquire_lock", return_value=None),
            mock.patch.object(coalesce.cache, "set", side_effect=ConnectionError("dragonfly is gone")),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertEqual(coalesce.coalesced(_POINT_KEY, lambda: "answer", ttl=60, wait_seconds=0), "answer")

        logged = _text(captured.records)
        self.assertIn("redata:context:", logged, "the key's kind was not kept")
        self.assertNotIn(_LATITUDE, logged)

    def test_a_lock_released_twice_does_not_log_the_point(self) -> None:
        cache.delete(f"{_POINT_KEY}:flight")
        with self.assertLogs("urbanlens", level="DEBUG") as captured:
            locks.release_lock(f"{_POINT_KEY}:flight", "a-token")

        self.assertNotIn(_LATITUDE, _text(captured.records))

    def test_a_key_naming_nothing_is_logged_as_it_is(self) -> None:
        self.assertEqual(redact_cache_key("ul:beat:sweep-held-uploads"), "ul:beat:sweep-held-uploads")

    def test_the_same_key_draws_the_same_token(self) -> None:
        """So one failing key can still be followed through the log."""
        self.assertEqual(redact_cache_key(_POINT_KEY), redact_cache_key(_POINT_KEY))
        self.assertNotEqual(redact_cache_key(_POINT_KEY), redact_cache_key("redata:context:40.00000,-75.00000"))


class AMapTileSomeoneViewedTests(RedataConfiguredMixin, TestCase):
    """At zoom 18 a tile is a building; its zoom-12 ancestor is a town."""

    def test_the_tile_index_is_coarsened(self) -> None:
        self.assertEqual(redact_tile(12, 1204, 1539), "12/1204/1539")
        self.assertEqual(redact_tile(18, 77096, 98496), "12/1204/1539 (z18)")

    def test_a_failed_basemap_tile_logs_only_its_coarse_tile(self) -> None:
        cache.clear()
        baker.make(User)
        self.client.force_login(baker.make(User))
        url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 18, "x": 77096, "y": 98496})
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=True,
            ),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway.download_tile",
                return_value=(500, b"", "text/plain"),
            ),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertEqual(self.client.get(url).status_code, 503)

        logged = _text(captured.records)
        self.assertIn("12/1204/1539", logged)
        self.assertNotIn("77096", logged)
        self.assertNotIn("98496", logged)


class AHistoricalMapTileCallTests(SimpleTestCase):
    """Each REData call is recorded in ``ApiCallLog`` by its endpoint, and an overlay tile's path is the tile."""

    def test_its_recorded_endpoint_stops_at_the_georeference(self) -> None:
        url = "https://redata.example.test/api/v1/maps/georeferences/abc-123/tiles/18/77096/98496.png"

        endpoint = RedataHistoricalMapsGateway.endpoint_for_log(url)

        self.assertEqual(endpoint, "https://redata.example.test/api/v1/maps/georeferences/abc-123/tiles/")


class ALoginCounterTests(TestCase):
    """A failed login for an identifier that names no account is counted under what was typed: a username, an email
    address, or now and then a password typed into the wrong field."""

    _TYPED = "quokka.keeper@example.org"

    def _store_down(self) -> ExitStack:
        broken = mock.Mock()
        broken.peek_int.side_effect = ConnectionError("dragonfly is gone")
        stack = ExitStack()
        stack.enter_context(mock.patch.object(counters, "_ops", return_value=broken))
        stack.enter_context(mock.patch.object(counters, "_last_warning", float("-inf")))
        return stack

    def test_its_key_does_not_hold_what_was_typed(self) -> None:
        key = _lockout_key_for_identifier(self._TYPED)

        self.assertNotIn("quokka", key)
        self.assertEqual(
            key,
            _lockout_key_for_identifier(" Quokka.Keeper@Example.org "),
            "two spellings of one address drew different keys",
        )

    def test_a_store_outage_does_not_log_it(self) -> None:
        with self._store_down(), self.assertLogs("urbanlens", level="DEBUG") as captured:
            self.assertFalse(_is_locked_out(_lockout_key_for_identifier(self._TYPED)))

        logged = _text(captured.records)
        self.assertIn("login_lockout", logged, "the outage was not logged with the counter's kind")
        self.assertNotIn("quokka", logged)

    def test_a_refused_count_does_not_carry_its_key(self) -> None:
        """A throttle key ends with the client's address."""
        with self._store_down(), self.assertRaises(counters.CounterUnavailableError) as raised:
            counters.peek("ul:throttle:login:29112233:203.0.113.9", on_outage=counters.Outage.REFUSE)

        self.assertIn("ul:throttle:login", str(raised.exception))
        self.assertNotIn("203.0.113.9", str(raised.exception))


class AnAddressAMailServerRefusedTests(SimpleTestCase):
    """A refusal names the recipients it refused, and a failed send is logged with its traceback."""

    def test_the_address_is_not_in_the_log(self) -> None:
        address = "quokka.keeper@example.org"
        refusal = smtplib.SMTPRecipientsRefused({address: (550, f"5.1.1 <{address}>: no such user".encode())})
        with (
            mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=refusal),
            handler_output("urbanlens") as output,
        ):
            send_email_now(to=address, subject="Hello", text_body="Hello")

        logged = output.getvalue()
        self.assertIn("SMTPRecipientsRefused", logged, "the failure was not logged")
        self.assertNotIn("quokka.keeper", logged)


class APlacesOtherNamesTests(TestCase):
    """An alias is another name for the place and a link is often a page about it; refusals are logged as raised."""

    def test_no_refusal_quotes_the_name_or_the_link(self) -> None:
        profile = Profile.objects.get(user=baker.make(User))
        pin = baker.make(Pin, profile=profile, location=baker.make(Location), name=_MARKER)
        current_name, _created = PinAlias.objects.get_or_create(pin=pin, name=_MARKER)
        create_pin_alias(pin, name="Old Quokkabridge Ward")
        create_pin_link(pin, name="history", url="https://example.org/quokkabridge")

        for refused in (
            lambda: create_pin_alias(pin, name="Old Quokkabridge Ward"),
            lambda: delete_pin_alias(pin, current_name),
            lambda: create_pin_link(pin, name="history", url="https://example.org/quokkabridge"),
            lambda: create_pin_link(pin, name="history", url="ftp://quokkabridge.example.org/"),
        ):
            with self.assertRaises(PinSubResourceError) as raised:
                refused()
            with self.subTest(error=type(raised.exception).__name__):
                self.assertIn(str(pin.pk), str(raised.exception))
                self.assertNotIn("quokkabridge", str(raised.exception).casefold())


class AMapsLinkThatWouldNotParseTests(TestCase):
    """A saved-places link carries the place's name and its coordinates."""

    def test_it_is_not_logged(self) -> None:
        url = f"https://www.google.com/maps/place/Quokkabridge+Sanatorium/@{_LATITUDE},{_LONGITUDE},17z"
        profile = Profile.objects.get(user=baker.make(User))
        with (
            mock.patch.object(GoogleGeocodingGateway, "extract_coordinates_from_url", side_effect=ValueError("no")),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            GoogleMapsGateway(api_key="test-key").resolve_preview_rows(
                [{"stem": "Saved", "maps_url": url, "needs_lookup": True}], profile, room=5
            )

        logged = _text(captured.records)
        self.assertIn("Failed to extract coordinates", logged, "the failure was not logged")
        self.assertNotIn("Quokkabridge", logged)
        self.assertNotIn(_LATITUDE, logged)
