"""A new wiki is enriched only on behalf of someone who allowed outbound lookups.

Enrichment sends the coordinate to Google, REData, Overpass and Overture. A pin whose owner turned
``external_apis_enabled`` off still gives its location a wiki, which is local, but must not cause those calls.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

ENQUEUE = "urbanlens.dashboard.services.core.bulk_followup.enqueue_follow_on"


class WikiEnrichmentConsentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(
            Location, official_name="", latitude="41.63", longitude="-73.83", place_resolved_at=None
        )

    def _pin(self, *, external_apis_enabled: bool) -> Pin:
        pin = baker.make(Pin, location=self.location, parent_pin=None)
        pin.profile.external_apis_enabled = external_apis_enabled
        pin.profile.save(update_fields=["external_apis_enabled"])
        return pin

    def _enrichments(self) -> list[int]:
        with mock.patch(ENQUEUE) as enqueue:
            tasks.ensure_wiki_for_location(self.location.pk)
        return [call.args[2] for call in enqueue.call_args_list if call.args[0] is tasks.enrich_wiki_location]

    def test_an_opted_out_owner_still_gets_a_wiki(self) -> None:
        self._pin(external_apis_enabled=False)
        self._enrichments()
        self.assertTrue(Wiki.objects.filter(location=self.location).exists())

    def test_an_opted_out_owner_causes_no_enrichment(self) -> None:
        self._pin(external_apis_enabled=False)
        self.assertEqual(self._enrichments(), [])

    def test_an_opted_in_owner_still_causes_enrichment(self) -> None:
        self._pin(external_apis_enabled=True)
        wiki_pks = self._enrichments()
        self.assertEqual(wiki_pks, [Wiki.objects.get(location=self.location).pk])

    def test_a_later_opted_in_pin_enriches_the_wiki_an_opted_out_pin_created(self) -> None:
        self._pin(external_apis_enabled=False)
        self.assertEqual(self._enrichments(), [])
        self._pin(external_apis_enabled=True)
        self.assertEqual(self._enrichments(), [Wiki.objects.get(location=self.location).pk])

    def test_a_wiki_whose_chain_has_run_is_not_enriched_again(self) -> None:
        self._pin(external_apis_enabled=True)
        self._enrichments()
        with mock.patch("urbanlens.dashboard.services.locations.boundaries.boundary_generation_ran", return_value=True):
            self.assertEqual(self._enrichments(), [])
