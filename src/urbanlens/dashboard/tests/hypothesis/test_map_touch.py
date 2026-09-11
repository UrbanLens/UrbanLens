"""A pin whose map appearance changed has to say so, whatever changed it.

The client does not read the server's pin cache. It polls `map.pins.meta`, which
reports `Max(Pin.updated)` over the profile's root pins, and refetches only when
that moves. So every write that changes what a pin *draws* has two obligations:
drop the server's cached copy, and move `Pin.updated`.

Label `order` decides which of a pin's labels supplies its icon and colour
(`_winning_display_label` sorts by `-order`), so a reorder changes what a pin
draws without touching the pin at all. Four paths reorder labels in bulk. All
four dropped the server cache; none moved `Pin.updated`, so the browser kept
drawing the old icon until its own cache expired (P106).

The four are tested through their real entry points rather than through the
shared helper they call, because "every one of them calls the helper" is the
thing that was already true while the bug was live.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.map_pins import MapPinCache


class LabelWritesTellTheClientTests(TestCase):
    """Every bulk label write moves the timestamp the client polls."""

    profile: Profile
    pin: Pin
    labels: list[Label]

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.labels = [
            baker.make(Label, profile=self.profile, kind="tag", name=f"tag-{index}", order=index) for index in range(3)
        ]
        location = baker.make(Location, latitude=42.6, longitude=-73.7)
        self.pin = baker.make(Pin, profile=self.profile, location=location)
        self.pin.labels.add(*self.labels)
        self.client.force_login(self.user)

    def _updated(self) -> object:
        """The pin's stored `updated`, read fresh.

        Returns:
            The timestamp the client's poll is derived from.
        """
        return Pin.objects.filter(pk=self.pin.pk).values_list("updated", flat=True).first()

    def _api_key(self) -> str:
        """A key scoped to write labels.

        Returns:
            The raw key, for an `Authorization: Bearer` header.
        """
        _key, raw_key = generate_api_key(self.user, "touch test")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[ApiKeyScope.LABELS_READ.value, ApiKeyScope.LABELS_WRITE.value]
        )
        return raw_key

    def _reversed_ids(self) -> list[int]:
        """Label ids in an order that is genuinely different from the stored one.

        Returns:
            The label primary keys, reversed.
        """
        return [label.pk for label in reversed(self.labels)]

    def test_the_dashboard_reorder_moves_it(self) -> None:
        before = self._updated()

        response = self.client.post(
            reverse("label.reorder", kwargs={"label_kind": "tags"}),
            data=json.dumps({"tag_ids": self._reversed_ids()}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(self._updated(), before, "LabelReorderView left the client polling an unchanged timestamp")

    def test_the_organize_page_reorder_moves_it(self) -> None:
        before = self._updated()

        response = self.client.post(
            reverse("organize.priority.save"),
            data=json.dumps({"items": [{"id": label_id} for label_id in self._reversed_ids()]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(
            self._updated(), before, "OrganizePrioritySaveView left the client polling an unchanged timestamp"
        )

    def test_the_api_reorder_moves_it(self) -> None:
        raw_key = self._api_key()
        before = self._updated()

        response = self.client.post(
            reverse("external_api:labels.reorder"),
            data=json.dumps({"uuids": [str(label.uuid) for label in reversed(self.labels)]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(self._updated(), before, "the API's reorder left the client polling an unchanged timestamp")

    def test_the_api_bulk_edit_moves_it(self) -> None:
        raw_key = self._api_key()
        before = self._updated()

        response = self.client.post(
            reverse("external_api:labels.bulk.edit"),
            data=json.dumps({"uuids": [str(self.labels[0].uuid)], "color": "#2196F3"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw_key}",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(
            self._updated(), before, "the API's bulk edit left the client polling an unchanged timestamp"
        )

    def test_the_meta_endpoint_the_client_polls_moves_too(self) -> None:
        """End to end: the reorder has to be visible in what the browser actually reads."""
        before = self.client.get(reverse("map.pins.meta")).json()["last_updated"]

        self.client.post(
            reverse("label.reorder", kwargs={"label_kind": "tags"}),
            data=json.dumps({"tag_ids": self._reversed_ids()}),
            content_type="application/json",
        )

        after = self.client.get(reverse("map.pins.meta")).json()["last_updated"]
        self.assertNotEqual(after, before, "map.pins.meta reported no change, so the client would never refetch")


class TouchingOnlyReachesTheRightPinsTests(TestCase):
    """The bump is scoped, or it invalidates every client's cache on every edit."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_a_pin_without_the_label_is_left_alone(self) -> None:
        from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels

        labels = [baker.make(Label, profile=self.profile, kind="tag", name=f"t{i}", order=i) for i in range(2)]
        carrying = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        carrying.labels.add(labels[0])
        untouched = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=2.0, longitude=2.0))
        before = Pin.objects.filter(pk=untouched.pk).values_list("updated", flat=True).first()

        touched = touch_pins_for_labels([labels[0].pk])

        self.assertEqual(touched, 1)
        self.assertEqual(Pin.objects.filter(pk=untouched.pk).values_list("updated", flat=True).first(), before)

    def test_a_pin_carrying_two_changed_labels_is_bumped_once(self) -> None:
        """`labels__in` joins, so without a distinct subquery this counts the pin twice."""
        from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels

        labels = [baker.make(Label, profile=self.profile, kind="tag", name=f"t{i}", order=i) for i in range(2)]
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        pin.labels.add(*labels)

        self.assertEqual(touch_pins_for_labels([label.pk for label in labels]), 1)

    def test_clearing_a_customization_moves_it(self) -> None:
        """A delete fires no `post_save`, so this path has to say so for itself."""
        from urbanlens.dashboard.services.labels.customization import (
            clear_label_customization,
            upsert_label_customization,
        )

        label = baker.make(Label, profile=self.profile, kind="tag", name="t", order=1)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        pin.labels.add(label)
        upsert_label_customization(self.profile, label, name=None, icon="star", color=None)
        before = Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

        clear_label_customization(self.profile, label)

        self.assertGreater(Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first(), before)

    def test_setting_a_customization_moves_it(self) -> None:
        from urbanlens.dashboard.services.labels.customization import upsert_label_customization

        label = baker.make(Label, profile=self.profile, kind="tag", name="t", order=1)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        pin.labels.add(label)
        before = Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

        upsert_label_customization(self.profile, label, name=None, icon="star", color=None)

        self.assertGreater(Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first(), before)

    def test_a_customization_does_not_reach_another_profiles_pins(self) -> None:
        """Globals are carried by everyone, so an override must stay scoped to one profile."""
        from urbanlens.dashboard.services.labels.customization import upsert_label_customization

        shared = baker.make(Label, profile=None, kind="tag", name="global", order=1)
        mine = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        mine.labels.add(shared)
        stranger = baker.make(User).profile
        theirs = baker.make(Pin, profile=stranger, location=baker.make(Location, latitude=3.0, longitude=3.0))
        theirs.labels.add(shared)
        before = Pin.objects.filter(pk=theirs.pk).values_list("updated", flat=True).first()

        upsert_label_customization(self.profile, shared, name=None, icon="star", color=None)

        self.assertEqual(Pin.objects.filter(pk=theirs.pk).values_list("updated", flat=True).first(), before)

    def test_no_labels_is_not_a_full_table_update(self) -> None:
        from urbanlens.dashboard.services.map_pins.touch import touch_pins_for_labels

        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        before = Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

        self.assertEqual(touch_pins_for_labels([]), 0)
        self.assertEqual(Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first(), before)


class TheOtherWritesThatChangeAPinsAppearanceTests(TestCase):
    """The same obligation, for the triggers that are not label edits.

    A pin's payload carries its labels and its rating. Both can change without
    the pin row being written - `pin.labels.add()` writes the through table, and
    a `Review` is its own row - and both already drop the server's cached copy
    through receivers in `models/pin/signals.py`. Whether they also move
    `Pin.updated`, which is the half P106 was about, is what these ask.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))

    def _updated(self) -> object:
        """The pin's stored `updated`, read fresh.

        Returns:
            The timestamp the client's poll is derived from.
        """
        return Pin.objects.filter(pk=self.pin.pk).values_list("updated", flat=True).first()

    def test_adding_a_label_to_a_pin_moves_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="added", order=1)
        before = self._updated()

        self.pin.labels.add(label)

        self.assertGreater(self._updated(), before, "the pin gained a chip the client will never fetch")

    def test_removing_a_label_from_a_pin_moves_it(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="removed", order=1)
        self.pin.labels.add(label)
        before = self._updated()

        self.pin.labels.remove(label)

        self.assertGreater(self._updated(), before, "the pin lost a chip the client will keep drawing")

    def test_rating_a_pin_moves_it(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        before = self._updated()

        baker.make(Review, pin=self.pin, profile=self.profile, rating=4)

        self.assertGreater(self._updated(), before, "the pin's rating changed and the client is not told")

    def test_deleting_a_rating_moves_it(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        review = baker.make(Review, pin=self.pin, profile=self.profile, rating=4)
        before = self._updated()

        review.delete()

        self.assertGreater(self._updated(), before, "the pin's rating was removed and the client is not told")


class TheCachedPinsAreActuallyDroppedTests(TestCase):
    """That the drop reaches Valkey, not just that the code path runs.

    `clear_for_profiles` builds its own connection, which in this suite the
    network guard refuses - and the caller swallows that, deliberately, because a
    receiver that dies on an unavailable cache breaks writes that have nothing to
    do with caching. Swallowing it also means a drop that never happened looks
    exactly like one that did, so this patches a fake in and reads the keys back.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.redis = FakeRedis()

    def test_editing_a_label_drops_the_carrying_profiles_cached_pins(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="carried", order=1)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        pin.labels.add(label)
        cache = MapPinCache(self.profile, client=self.redis)
        cache.rebuild(Pin.objects.filter(profile=self.profile).root_pins().select_related("location"))
        self.assertTrue(self.redis.exists(cache.pins_key), "the fake cache did not warm, so this asserts nothing")

        with (
            mock.patch.object(MapPinCache, "make_client", classmethod(lambda cls: self.redis)),
            self.captureOnCommitCallbacks(execute=True),
        ):
            label.color = "#2196F3"
            label.save(update_fields=["color"])

        self.assertFalse(self.redis.exists(cache.pins_key), "the cached pin set survived a label edit")
        self.assertFalse(self.redis.exists(cache.meta_key), "the cache metadata survived a label edit")

    def test_an_unreachable_cache_does_not_break_the_edit(self) -> None:
        """The write must succeed even when the cache cannot be dropped."""
        label = baker.make(Label, profile=self.profile, kind="tag", name="carried", order=1)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        pin.labels.add(label)
        before = Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

        def refuse(cls: type[MapPinCache]) -> None:
            raise RuntimeError("cache is unreachable")

        with (
            mock.patch.object(MapPinCache, "make_client", classmethod(refuse)),
            self.captureOnCommitCallbacks(execute=True),
        ):
            label.color = "#2196F3"
            label.save(update_fields=["color"])

        self.assertGreater(Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first(), before)
