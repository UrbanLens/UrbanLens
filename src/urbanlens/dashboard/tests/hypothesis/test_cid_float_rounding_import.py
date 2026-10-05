"""A Takeout pin's 64-bit CID survives the import wizard's browser round trip (REData P116).

The preview reaches the browser as JSON and the confirm step posts the pins back. A CID sent as a
JSON number came back as a JavaScript Number's shortest digits padded with zeros
(14522379626423718452 -> 14522379626423718000): REData stored 1,945 such ids on 2026-07-31 and
asked Google about them every night after.
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any
from unittest import mock

from django.core.serializers.json import DjangoJSONEncoder
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure
from urbanlens.dashboard.services.apis.locations.cid_resolution import PROVIDER_REDATA, CidResolutionResult
from urbanlens.dashboard.services.apis.locations.cid_validation import MAX_CID, float_rounded
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway, confirmed_pin_cid
from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
from urbanlens.UrbanLens.settings.app import settings

#: Above 2**63, so a float64 cannot hold it: the browser hands back 14522379626423718000.
TRUE_CID = 0xC989DB53CE5B1234
MAPS_URL = f"https://www.google.com/maps/place/Willard+Asylum/data=!4m2!3m1!1s0x89d0a1b2c3d4e5f6:0x{TRUE_CID:x}"
LAT, LNG = 42.682512, -76.866421


def _through_the_browser(payload: object) -> Any:
    """What ``JSON.parse`` then ``JSON.stringify`` does to *payload*, as the server reads it back.

    Every JSON integer becomes a JavaScript Number, which prints its shortest round-trip digits.
    """
    return json.loads(
        json.dumps(payload, cls=DjangoJSONEncoder), parse_int=lambda digits: int(Decimal(repr(float(digits))))
    )


def _wire(payload: object) -> Any:
    """*payload* as REData's JSON parser reads it."""
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _fake_redata(entries: list[Any]) -> dict[str, Any]:
    """REData's ``resolve-cids`` answer: a cid in a feature id wins over a claimed one, and keys the result."""
    results: dict[str, Any] = {}
    for raw in entries:
        url = raw.get("url", "") if isinstance(raw, dict) else ""
        claimed = raw.get("cid") if isinstance(raw, dict) else raw
        cid = int(url.rsplit(":0x", 1)[1], 16) if ":0x" in url else int(claimed)
        results[str(cid)] = {"lat": LAT, "lng": LNG} if cid == TRUE_CID else None
    return {"results": results, "pending": []}


class TakeoutCidSurvivesTheBrowserTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def _confirmed_lists_after_the_browser(self) -> list[dict[str, Any]]:
        pins = GoogleMapsGateway._preview_pins(
            [{"latitude": 42.68, "longitude": -76.86, "name": "Willard Asylum", "cid": TRUE_CID, "maps_url": MAPS_URL}],
            self.profile,
        )
        preview = _through_the_browser({"lists": [{"stem": "Saved", "pins": pins}]})
        # import-wizard.ts's confirm payload, field for field.
        confirmed = [
            {
                "stem": lst["stem"],
                "create_category": False,
                "label_ids": [],
                "pins": [
                    {
                        "name": p["name"],
                        "lat": p["lat"],
                        "lng": p["lng"],
                        "description": p["description"],
                        "cid": p["cid"],
                        "maps_url": p["maps_url"],
                        "label_ids": [],
                    }
                    for p in lst["pins"]
                ],
            }
            for lst in preview["lists"]
        ]
        return _through_the_browser(confirmed)

    def test_a_cid_above_2_to_the_53_is_placed_at_its_resolved_coordinates(self) -> None:
        confirmed_lists = self._confirmed_lists_after_the_browser()
        gateway = GoogleMapsGateway(api_key="test-key")
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            list(gateway.iter_confirmed_import_events(confirmed_lists, self.profile, auto_tag=False))
        deferred_lists = json.loads(json.dumps(enqueue.call_args.args[2]))  # Celery's JSON serializer.

        posted: list[Any] = []

        def post(_url: str, *, json: Any, **_kwargs: Any) -> mock.Mock:
            body = _wire(json)
            posted.extend(body["cids"])
            return mock.Mock(status_code=200, json=mock.Mock(return_value=_fake_redata(body["cids"])))

        session = mock.Mock(post=mock.Mock(side_effect=post))
        with (
            mock.patch.object(settings, "redata_api_url", "https://redata.example.test"),
            mock.patch.object(settings, "redata_api_key", "test-key"),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.RedataCidGateway",
                side_effect=lambda: RedataCidGateway(
                    base_url="https://redata.example.test", api_key="test-key", session=session
                ),
            ),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as retry,
        ):
            tasks.resolve_deferred_pin_locations(self.profile.pk, deferred_lists, auto_tag=False)

        retry.assert_not_called()
        self.assertEqual([int(entry["cid"]) for entry in posted], [TRUE_CID])
        pin = Pin.objects.get(profile=self.profile)
        self.assertAlmostEqual(float(pin.effective_latitude), LAT, places=5)
        self.assertEqual(pin.location.cid, TRUE_CID)


def _pin(**fields: Any) -> dict[str, Any]:
    return {"name": "Willard Asylum", "lat": 42.68, "lng": -76.86, "description": "", "label_ids": [], **fields}


class PreviewCidRoundTripTests(SimpleTestCase):
    @given(st.integers(min_value=1, max_value=MAX_CID))
    def test_every_cid_survives_preview_browser_and_confirm(self, cid: int) -> None:
        url = f"https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x1:0x{cid:x}"
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.google.maps.preview_needs_legacy_repair", return_value=False
        ):
            pins = GoogleMapsGateway._preview_pins(
                [{"latitude": 42.0, "longitude": -76.0, "name": "X", "cid": cid, "maps_url": url}], mock.Mock()
            )
            bare = GoogleMapsGateway._preview_pins(
                [{"latitude": 42.0, "longitude": -76.0, "name": "X", "cid": cid}], mock.Mock()
            )

        self.assertEqual(confirmed_pin_cid(_through_the_browser(pins)[0]), cid)
        self.assertEqual(int(_through_the_browser(bare)[0]["cid"]), cid)


class ConfirmStepCidTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, pins: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], mock.Mock]:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            events = list(
                self.gateway.iter_confirmed_import_events(
                    [{"stem": "", "create_category": False, "label_ids": [], "pins": pins}],
                    self.profile,
                    auto_tag=False,
                )
            )
        return events, enqueue

    def test_a_rounded_claim_is_replaced_by_its_urls_cid(self) -> None:
        _events, enqueue = self._run([_pin(cid=float_rounded(TRUE_CID), maps_url=MAPS_URL)])

        self.assertEqual(enqueue.call_args.args[2][0]["pins"][0]["cid"], TRUE_CID)

    def test_a_rounded_claim_with_no_url_is_skipped_and_sent_nowhere(self) -> None:
        events, enqueue = self._run([_pin(cid=float_rounded(TRUE_CID))])

        enqueue.assert_not_called()
        self.assertEqual([e["outcome"] for e in events if e["type"] == "progress"], ["skipped"])
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())

    def test_a_pin_claiming_no_cid_keeps_none_even_when_its_url_states_one(self) -> None:
        for claimed in (None, "", 0, "0"):
            with self.subTest(claimed=claimed):
                self.assertIsNone(confirmed_pin_cid(_pin(cid=claimed, maps_url=MAPS_URL)))
        self.assertIsNone(confirmed_pin_cid(_pin(maps_url="https://maps.google.com/?cid=12345")))

    def test_a_zero_cid_feature_id_previews_as_no_cid(self) -> None:
        pins = GoogleMapsGateway._preview_pins(
            [{"latitude": 42.0, "longitude": -76.0, "name": "X", "cid": 0}], self.profile
        )

        self.assertIsNone(pins[0]["cid"])

    def test_a_claim_naming_another_place_than_its_url_is_skipped(self) -> None:
        events, enqueue = self._run([_pin(cid=str(TRUE_CID - 1), maps_url=MAPS_URL), _pin(cid=1.5), _pin(cid=True)])

        enqueue.assert_not_called()
        self.assertEqual([e["outcome"] for e in events if e["type"] == "progress"], ["skipped", "skipped", "skipped"])


class DeferredLookupRejectionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def _deferred(self, *pins: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"stem": "", "create_category": False, "label_ids": [], "pins": list(pins)}]

    def test_a_cid_reData_refuses_becomes_a_failure_at_once_and_is_not_retried(self) -> None:
        url = "https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x1:0x8"
        result = CidResolutionResult(
            provider=PROVIDER_REDATA, resolved={7: (LAT, LNG)}, rejected={8: "cid_conflicts_with_url"}
        )
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids", return_value=result),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as retry,
        ):
            tasks.resolve_deferred_pin_locations(
                self.profile.pk, self._deferred(_pin(cid=7), _pin(cid=8, maps_url=url)), auto_tag=False
            )

        retry.assert_not_called()
        failure = PinImportFailure.objects.get(profile=self.profile)
        self.assertEqual((int(failure.cid), failure.maps_url), (8, url))

    def test_a_retry_queued_before_the_fix_is_looked_up_by_its_urls_cid(self) -> None:
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.cid_resolution.resolve_cids",
                return_value=CidResolutionResult(provider=PROVIDER_REDATA, resolved={TRUE_CID: (LAT, LNG)}),
            ) as resolve_cids,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
            mock.patch.object(tasks.resolve_deferred_pin_locations, "retry") as retry,
        ):
            tasks.resolve_deferred_pin_locations(
                self.profile.pk,
                self._deferred(
                    _pin(cid=float_rounded(TRUE_CID), maps_url=MAPS_URL), _pin(cid=float_rounded(TRUE_CID + 10**9))
                ),
                auto_tag=False,
            )

        retry.assert_not_called()
        self.assertEqual(resolve_cids.call_args.args[0], [TRUE_CID])
        self.assertTrue(Pin.objects.filter(profile=self.profile).exists())
