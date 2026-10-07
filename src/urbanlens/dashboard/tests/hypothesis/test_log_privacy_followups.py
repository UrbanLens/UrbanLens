"""What a model wrote, where a building or a cached lookup is, and which map tile someone looked at stay out of log lines.

Pin names name undisclosed sites and coordinates locate them (``services.security.redact``); a trivia question is
generated from a wiki's text, a lock key can carry the point it locks, and a high-zoom tile index is a building.
"""

from __future__ import annotations

import logging
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core import coalesce, locks
from urbanlens.dashboard.services.pins.pin_restructure import building_footprint
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
