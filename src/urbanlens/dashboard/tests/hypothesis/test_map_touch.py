"""A pin whose map appearance changed has to say so, whatever changed it.

The client polls `map.pins.meta`, which reports a fingerprint over the profile's
root pins, and refetches only when that moves. So every write that changes what a
pin *draws* has to move `Pin.updated` - including the writes that never touch the
pin row.

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

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


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


class DeletingALabelTellsTheClientTests(TestCase):
    """A deleted label leaves a chip on the map until something says otherwise.

    Deleting a `Label` cascades its through rows in SQL, which fires no
    `m2m_changed` and writes no `auto_now` column - so the pins that carried it
    look untouched, the fingerprint the client polls does not move, and the
    browser goes on drawing a chip for a label that no longer exists. The same
    hole as P106's seven, found while normalising labels out of the payload.

    `pre_delete` rather than `post_delete`: by the time the row is gone so are
    the through rows, and there is no longer any way to ask which pins carried
    it.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.label = baker.make(Label, profile=self.profile, kind="tag", name="Doomed", order=1)
        self.carrying = baker.make(
            Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0)
        )
        self.carrying.labels.add(self.label)

    def _updated(self, pin: Pin) -> object:
        """The pin's stored `updated`, read fresh.

        Args:
            pin: Whose stamp to read.

        Returns:
            The timestamp the client's poll is derived from.
        """
        return Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

    def test_deleting_a_label_moves_it(self) -> None:
        before = self._updated(self.carrying)

        self.label.delete()

        self.assertGreater(
            self._updated(self.carrying), before, "the pin kept a chip for a label that no longer exists"
        )

    def test_deleting_through_a_queryset_moves_it_too(self) -> None:
        """The bulk paths delete without ever holding an instance."""
        before = self._updated(self.carrying)

        Label.objects.filter(pk=self.label.pk).delete()

        self.assertGreater(self._updated(self.carrying), before)

    def test_a_pin_that_never_carried_it_is_left_alone(self) -> None:
        other = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=2.0, longitude=2.0))
        before = self._updated(other)

        self.label.delete()

        self.assertEqual(self._updated(other), before)

    def test_the_fingerprint_the_client_polls_moves(self) -> None:
        """End to end: it has to be visible in what the browser actually reads."""
        before = self.client.get(reverse("map.pins.meta")).json()["fingerprint"]

        self.label.delete()

        self.assertNotEqual(self.client.get(reverse("map.pins.meta")).json()["fingerprint"], before)

    def test_deleting_a_customization_row_directly_moves_it(self) -> None:
        """`clear_label_customization` says so for itself; a raw delete did not."""
        from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
        from urbanlens.dashboard.services.labels.customization import upsert_label_customization

        upsert_label_customization(self.profile, self.label, name=None, icon="star", color=None)
        before = self._updated(self.carrying)

        LabelCustomization.objects.filter(profile=self.profile, label=self.label).delete()

        self.assertGreater(self._updated(self.carrying), before)


class WritingTheLabelSideOfTheRelationTellsTheClientTests(TestCase):
    """`label.pins.add(pin)` is the same write as `pin.labels.add(label)`.

    In the reverse direction `m2m_changed` hands the receiver the *Label* as
    `instance` and the *pin* ids in `pk_set`. A receiver that reads
    `instance.pk` as a pin therefore touches whichever pin happens to share that
    number, and never touches the pins that actually changed.

    Two live callers write this way: `services.labels.merge` moving a source
    label's pins onto the target, and the undo handler restoring a deleted
    label's assignments.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _updated(self, pin: Pin) -> object:
        """The pin's stored `updated`, read fresh.

        Args:
            pin: Whose stamp to read.

        Returns:
            The timestamp the client's poll is derived from.
        """
        return Pin.objects.filter(pk=pin.pk).values_list("updated", flat=True).first()

    def test_adding_pins_from_the_label_side_moves_them(self) -> None:
        label = baker.make(Label, profile=self.profile, kind="tag", name="Reverse", order=1)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=1.0, longitude=1.0))
        before = self._updated(pin)

        label.pins.add(pin)

        self.assertGreater(self._updated(pin), before, "the pin gained a chip the client will never fetch")

    def test_it_does_not_touch_the_pin_that_shares_the_labels_number(self) -> None:
        """The sharp edge: reading `instance.pk` as a pin id in this direction.

        A pin is created at exactly the label's primary key, so a receiver making
        that mistake writes to it and this sees it.
        """
        label = baker.make(Label, profile=self.profile, kind="tag", name="Collider", order=1)
        impostor = Pin.objects.create(
            pk=label.pk,
            profile=self.profile,
            location=baker.make(Location, latitude=5.0, longitude=5.0),
            name="Shares the label's number",
        )
        target = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=6.0, longitude=6.0))
        before = self._updated(impostor)

        label.pins.add(target)

        self.assertEqual(self._updated(impostor), before, "a pin that gained nothing was marked as changed")

    def test_merging_two_labels_moves_the_pins_that_moved(self) -> None:
        from urbanlens.dashboard.services.labels.merge import merge_labels

        source = baker.make(Label, profile=self.profile, kind="tag", name="Source", order=1)
        target = baker.make(Label, profile=self.profile, kind="tag", name="Target", order=2)
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=3.0, longitude=3.0))
        pin.labels.add(source)
        before = self._updated(pin)

        merge_labels(target=target, sources=[source], profile=self.profile)

        self.assertGreater(self._updated(pin), before, "the pin's chips changed and the client is not told")


class TheOtherWritesThatChangeAPinsAppearanceTests(TestCase):
    """The same obligation, for the triggers that are not label edits.

    A pin's payload carries its labels and its rating. Both can change without
    the pin row being written - `pin.labels.add()` writes the through table, and
    a `Review` is its own row.
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
