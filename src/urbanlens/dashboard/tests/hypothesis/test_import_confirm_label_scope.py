"""A confirmed import may attach only the labels its importer could have picked.

The import dialog offers ``Label.objects.visible_to(profile).location_labels()``,
and the confirm step takes ``label_ids`` back from the client per list and per
pin. The map payload resolves a pin's labels by primary key, so a label that is
attached is also read back - name, icon and colour.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.cid_resolution import CidResolutionResult
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.tasks import _place_resolved_pins, run_confirmed_pin_import

FOREIGN_NAME = "Victim's private label"
OWN_NAME = "Importer's own label"


class ImportConfirmLabelScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.importer = baker.make(User).profile
        victim = baker.make(User).profile
        self.foreign = baker.make(Label, profile=victim, name=FOREIGN_NAME, kind=KIND_TAG)
        self.own = baker.make(Label, profile=self.importer, name=OWN_NAME, kind=KIND_TAG)
        self.shared = baker.make(Label, profile=None, name="A global label", kind=KIND_TAG)
        self.person = baker.make(Label, profile=self.importer, name="Someone I know", kind=KIND_USER)

    def _lists(self, *, list_label_ids: list[int], pin_label_ids: list[int], cid: int | None = None) -> list[dict]:
        pin = {
            "name": "Imported",
            "lat": 40.0,
            "lng": -74.0,
            "description": "",
            "cid": cid,
            "maps_url": "",
            "label_ids": pin_label_ids,
        }
        return [{"stem": "", "create_category": False, "label_ids": list_label_ids, "pins": [pin]}]

    def _stream(self, lists: list[dict]) -> Pin:
        list(GoogleMapsGateway(api_key="test-key").iter_confirmed_import_events(lists, self.importer, auto_tag=False))
        return Pin.objects.get(profile=self.importer, name="Imported")

    def _assert_only_pickable(self, pin: Pin) -> None:
        attached = set(pin.labels.values_list("pk", flat=True))
        self.assertIn(self.own.pk, attached, "the importer's own label must still attach")
        self.assertIn(self.shared.pk, attached, "a global label must still attach")
        self.assertNotIn(self.foreign.pk, attached, "another account's label was attached")
        self.assertNotIn(self.person.pk, attached, "a label kind the dialog never offers was attached")

    def test_a_list_label_is_held_to_what_the_importer_could_pick(self) -> None:
        pickable = [self.own.pk, self.shared.pk]
        pin = self._stream(self._lists(list_label_ids=[*pickable, self.foreign.pk, self.person.pk], pin_label_ids=[]))

        self._assert_only_pickable(pin)

    def test_a_pin_label_is_held_to_what_the_importer_could_pick(self) -> None:
        pickable = [self.own.pk, self.shared.pk]
        pin = self._stream(self._lists(list_label_ids=[], pin_label_ids=[*pickable, self.foreign.pk, self.person.pk]))

        self._assert_only_pickable(pin)

    def test_the_deferred_placement_applies_the_same_rule(self) -> None:
        cid = 4242
        every = [self.own.pk, self.shared.pk, self.foreign.pk, self.person.pk]
        lists = self._lists(list_label_ids=every, pin_label_ids=every, cid=cid)
        result = CidResolutionResult(provider="test", resolved={cid: (40.0, -74.0)})

        _place_resolved_pins(result, lists, profile=self.importer, auto_tag=False)

        self._assert_only_pickable(Pin.objects.get(profile=self.importer, name="Imported"))

    def test_a_foreign_label_is_never_read_back_through_the_importers_map(self) -> None:
        self.client.force_login(self.importer.user)
        lists = self._lists(list_label_ids=[self.foreign.pk, self.own.pk], pin_label_ids=[self.foreign.pk])

        with tasks_run_inline(run_confirmed_pin_import):
            response = self.client.post(
                reverse("pin.import.confirmed"),
                data=json.dumps({"lists": lists, "auto_tag": False}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 202, response.content)
        payload = self.client.get(reverse("map.pins")).content.decode()

        self.assertIn(OWN_NAME, payload, "the map payload carries no label names, so the next assertion proves nothing")
        self.assertNotIn(FOREIGN_NAME, payload)
