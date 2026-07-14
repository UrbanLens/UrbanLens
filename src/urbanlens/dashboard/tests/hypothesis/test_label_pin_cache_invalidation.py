"""Tests for a bug where editing a label's icon/color left the map pin cache stale.

A pin's rendered marker (icon/color) can come from a label it carries
(``Pin.effective_icon``/``effective_color``), not just its own fields. The
client's map pin cache only refreshes when the server's ``Max(Pin.updated)``
advances (see ``map_pins_meta`` in controllers/maps.py), but editing a label
never touched any Pin row - so a badge icon change was invisible to the
cache-freshness check and users kept seeing the old icon until something else
happened to invalidate the cache. LabelEditView/LabelCustomizeView now bump
``Pin.updated`` for every pin carrying the edited label.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin

_STALE = timezone.datetime(2020, 1, 1, tzinfo=timezone.get_default_timezone())


def _make_pin_with_label(profile, label) -> Pin:
    location = baker.make("dashboard.Location", latitude="40.500000", longitude="-74.500000")
    pin = baker.make("dashboard.Pin", profile=profile, location=location)
    pin.labels.add(label)
    Pin.objects.filter(pk=pin.pk).update(updated=_STALE)
    pin.refresh_from_db()
    return pin


class LabelEditPinCacheInvalidationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_editing_own_label_bumps_pins_carrying_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Urbex", icon="place")
        pin = _make_pin_with_label(self.profile, label)
        other_pin = baker.make("dashboard.Pin", profile=self.profile, location=baker.make("dashboard.Location", latitude="41.500000", longitude="-75.500000"))
        Pin.objects.filter(pk=other_pin.pk).update(updated=_STALE)

        url = reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id})
        response = self.client.post(url, data={"name": "Urbex", "icon": "explore"})
        self.assertEqual(response.status_code, 200)

        pin.refresh_from_db()
        other_pin.refresh_from_db()
        self.assertGreater(pin.updated, _STALE)
        self.assertEqual(other_pin.updated, _STALE)


class LabelCustomizePinCacheInvalidationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_customizing_a_global_label_bumps_own_pins_carrying_it(self) -> None:
        global_label = baker.make(Label, profile=None, kind="tag", name="Visited", icon="check")
        pin = _make_pin_with_label(self.profile, global_label)

        url = reverse("label.customize", kwargs={"label_kind": "tag", "label_id": global_label.id})
        response = self.client.post(url, data={"icon": "star", "color": "#ff0000"})
        self.assertEqual(response.status_code, 200)

        pin.refresh_from_db()
        self.assertGreater(pin.updated, _STALE)
