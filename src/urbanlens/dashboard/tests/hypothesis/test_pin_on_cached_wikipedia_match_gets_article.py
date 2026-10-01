"""A pin created where the Wikipedia match is already cached still gets its article.

Pin articles are seeded when a cache write turns a miss into a match. A second pin at the same
location arrives after that write, and the prefetch skips a fresh cache, so nothing seeded it."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.wiki.articles import get_article

_URL = "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital"
_MATCH = {
    "title": "Hudson River State Hospital",
    "extract": "<p>The <b>Hudson River State Hospital</b> is a former psychiatric hospital in Poughkeepsie, New York.</p>",
    "url": _URL,
    "page_id": 1,
}


class PinOnCachedMatchTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location, latitude=41.73328, longitude=-73.92812)
        with self.captureOnCommitCallbacks(execute=True):
            LocationCache.set(self.location, "wikipedia", _MATCH, query_key="hrsh")
        self.profile = baker.make(User).profile

    def _new_pin_and_prefetch(self) -> Pin:
        from urbanlens.dashboard import tasks

        pin = baker.make(Pin, profile=self.profile, location=self.location, name="my notes")
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=False,
            ),
            mock.patch("urbanlens.dashboard.services.locations.naming.update_location_name_from_external_sources"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            tasks.prefetch_location_external_data(self.location.pk, profile_id=self.profile.pk, pin_id=pin.pk)
        return pin

    def test_the_new_pin_gets_the_article_and_the_link(self) -> None:
        pin = self._new_pin_and_prefetch()

        article = get_article(pin=pin)
        self.assertIsNotNone(article)
        self.assertIn("Hudson River State Hospital", article.content)
        self.assertTrue(pin.links.filter(url=_URL).exists())

    def test_the_owners_opt_out_is_honoured(self) -> None:
        self.profile.auto_create_pin_article_from_wikipedia = False
        self.profile.save(update_fields=["auto_create_pin_article_from_wikipedia"])

        self.assertIsNone(get_article(pin=self._new_pin_and_prefetch()))

    def test_pin_creation_hands_the_prefetch_its_pin(self) -> None:
        from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            pin = create_pin_for_profile(self.profile, name="another", latitude=41.73328, longitude=-73.92812).pin

        prefetches = [
            call
            for call in enqueue.call_args_list
            if call.args and call.args[0].__name__ == "prefetch_location_external_data"
        ]
        self.assertEqual([call.kwargs.get("pin_id") for call in prefetches], [pin.pk])
