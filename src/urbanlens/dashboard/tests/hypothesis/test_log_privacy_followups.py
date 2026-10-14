"""
Where a building or a cached lookup is, which map tile someone looked at, and whose address or login it was stay out
of log lines.

Pin names name undisclosed sites and coordinates locate them (``services.security.redact``); a lock key can carry the
point it locks, a high-zoom tile index is a building, and a lockout counter is keyed by whatever a visitor typed.
"""

from __future__ import annotations

from contextlib import ExitStack
import logging
import smtplib
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.log_output import handler_output
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.account import _is_locked_out, _lockout_key_for_identifier
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
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

_MARKER = "Quokkabridge Sanatorium"
_LATITUDE, _LONGITUDE = "41.71825", "-73.93402"
_POINT_KEY = f"point:context:{_LATITUDE},{_LONGITUDE}"


def _text(records: list[logging.LogRecord]) -> str:
    """Every captured record as it would be written, traceback included."""
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


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
    """A shared lookup is keyed by the point it is about: ``<kind>:context:<lat>,<lng>``."""

    def test_a_failed_share_does_not_log_the_point(self) -> None:
        with (
            mock.patch.object(coalesce.cache, "get", return_value=None),
            mock.patch.object(coalesce, "acquire_lock", return_value=None),
            mock.patch.object(coalesce.cache, "set", side_effect=ConnectionError("dragonfly is gone")),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertEqual(coalesce.coalesced(_POINT_KEY, lambda: "answer", ttl=60, wait_seconds=0), "answer")

        logged = _text(captured.records)
        self.assertIn("point:context:", logged, "the key's kind was not kept")
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
        self.assertNotEqual(redact_cache_key(_POINT_KEY), redact_cache_key("point:context:40.00000,-75.00000"))


class AMapTileSomeoneViewedTests(SimpleTestCase):
    """At zoom 18 a tile is a building; its zoom-12 ancestor is a town."""

    def test_the_tile_index_is_coarsened(self) -> None:
        self.assertEqual(redact_tile(12, 1204, 1539), "12/1204/1539")
        self.assertEqual(redact_tile(18, 77096, 98496), "12/1204/1539 (z18)")


class ALoginCounterTests(TestCase):
    """
    A failed login for an identifier that names no account is counted under what was typed: a username, an email
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
