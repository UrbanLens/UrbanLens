"""Regression test: resolve_deferred_pin_locations forwards each pin's source
Google Maps URL to cid_resolution.resolve_cids.

REData's ``POST /places/resolve-cids/`` resolves via a place's own URL faster
and more reliably than the bare cid alone (see ``RedataCidGateway``'s
``CidLookupEntry``). A deferred pin queued from a Takeout CSV import carries
that URL in its dict under ``maps_url`` (see
``GoogleMapsGateway._csv_row_iter``) - this task must build ``urls_by_cid``
from it and pass it through, not just the bare list of cids.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult


class ResolveDeferredPinLocationsMapsUrlTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def test_maps_url_is_forwarded_as_urls_by_cid(self) -> None:
        url = "https://www.google.com/maps/place/Black+Point+Ruins/data=!4m2!3m1!1s0x0:0x3039"
        deferred_lists = [
            {
                "stem": "",
                "create_category": False,
                "label_ids": [],
                "pins": [
                    {
                        "name": "Black Point Ruins",
                        "lat": 41.348754,
                        "lng": -71.453896,
                        "description": "",
                        "cid": 12345,
                        "maps_url": url,
                    },
                    {"name": "No URL Place", "lat": 40.0, "lng": -74.0, "description": "", "cid": 67890},
                ],
            },
        ]

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids",
                return_value=CidResolutionResult(
                    provider=PROVIDER_REDATA, resolved={12345: (41.348754, -71.453896), 67890: (40.0, -74.0)}
                ),
            ) as resolve_cids,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)

        self.assertEqual(resolve_cids.call_args.kwargs["urls_by_cid"], {12345: url})

    def test_no_urls_forwards_an_empty_dict(self) -> None:
        deferred_lists = [
            {
                "stem": "",
                "create_category": False,
                "label_ids": [],
                "pins": [{"name": "No URL Place", "lat": 40.0, "lng": -74.0, "description": "", "cid": 67890}],
            },
        ]

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids",
                return_value=CidResolutionResult(provider=PROVIDER_REDATA, resolved={67890: (40.0, -74.0)}),
            ) as resolve_cids,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)

        self.assertEqual(resolve_cids.call_args.kwargs["urls_by_cid"], {})
