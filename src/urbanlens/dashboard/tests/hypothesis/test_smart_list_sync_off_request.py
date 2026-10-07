"""A pin change re-evaluates its owner's smart lists on the queue, never on the request (N30, Batch 1).

Below ``MAX_SMART_LISTS_PER_SYNC`` the old sync ran in the request's commit hook, once per pin per signal. Measured
on 2026-10-06 against 25 smart lists over 2,000 pins: one pin save cost 116 queries and 145-180 ms; a bulk edit of
50 pins cost 8,420 queries and 16 s; one of 500 pins (``MAX_BULK_PINS``) cost about 84,000 queries and 174 s.

A change now records a ``SmartListSyncRequest`` in its own transaction and, once it commits, queues one sync per
account. The sync evaluates every requested pin against each list in one query per rule. While requests are
outstanding, the account's smart lists say they are catching up.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem, SmartListSyncRequest
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins import smart_list_sync
from urbanlens.dashboard.services.pins.pin_bulk import BulkPinEdit, bulk_edit_pins

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "smart-list-sync-tests"}}

#: A square around (10, 10), for boundary lists.
_SQUARE = MultiPolygon(Polygon(((9, 9), (11, 9), (11, 11), (9, 11), (9, 9))), srid=4326)


@override_settings(CACHES=_LOCMEM)
class _SmartListCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _smart_lists(self, count: int, **rules) -> list[PinList]:
        """Lists whose filter matches anything named, so membership is never the variable."""
        rules = rules or {"smart_filter": {"name": "Anything"}}
        return [baker.make(PinList, profile=self.profile, is_smart=True, **rules) for _ in range(count)]

    def _pins(self, count: int, *, name: str = "Anything") -> list[Pin]:
        with mock.patch.object(smart_list_sync, "safely_enqueue_task"), self.captureOnCommitCallbacks(execute=True):
            pins = [baker.make(Pin, profile=self.profile, name=f"{name} {i}") for i in range(count)]
        SmartListSyncRequest.objects.all().delete()
        smart_list_sync.release_queued_claim(self.profile.pk)
        return pins

    def _rename(self, pin: Pin, name: str) -> None:
        pin.name = name
        pin.save(update_fields=["name", "updated"])


class TheRequestDoesNoMembershipWorkTests(_SmartListCase):
    def test_a_save_leaves_membership_to_the_queue(self) -> None:
        pin_lists = self._smart_lists(3)
        pin = self._pins(1, name="Nothing")[0]

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._rename(pin, "Anything at all")

        self.assertFalse(
            PinListItem.objects.filter(pin_list__in=pin_lists).exists(), "membership was decided inside the request"
        )
        self.assertTrue(SmartListSyncRequest.objects.filter(pin=pin).exists())
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[:2], (tasks.sync_requested_smart_lists, self.profile.pk))

    def test_the_request_costs_the_same_whatever_the_list_count(self) -> None:
        pin = self._pins(1)[0]

        def save_cost() -> int:
            with (
                mock.patch.object(smart_list_sync, "safely_enqueue_task"),
                CaptureQueriesContext(connection) as queries,
                self.captureOnCommitCallbacks(execute=True),
            ):
                self._rename(pin, "Anything again")
            smart_list_sync.release_queued_claim(self.profile.pk)
            return len(queries.captured_queries)

        self._smart_lists(1)
        save_cost()  # the first save after a list exists warms caches the comparison should not count
        one_list = save_cost()
        self._smart_lists(24)
        many_lists = save_cost()

        self.assertEqual(
            many_lists, one_list, f"25 smart lists cost the request {many_lists} queries where one cost {one_list}"
        )

    def test_an_account_without_smart_lists_records_nothing(self) -> None:
        pin = self._pins(1)[0]

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._rename(pin, "Anything else")

        self.assertFalse(SmartListSyncRequest.objects.exists())
        enqueue.assert_not_called()

    def test_a_bulk_edit_queues_one_sync(self) -> None:
        self._smart_lists(3)
        pins = self._pins(40)
        label = baker.make(Label, profile=self.profile, name="Extra", kind="tag")

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            bulk_edit_pins(self.profile, pins, BulkPinEdit(description="bulk", add_labels=[label]))

        self.assertEqual(enqueue.call_count, 1, f"40 pins queued {enqueue.call_count} syncs")
        self.assertEqual(set(SmartListSyncRequest.objects.values_list("pin_id", flat=True)), {pin.pk for pin in pins})

    def test_a_rolled_back_change_leaves_no_request(self) -> None:
        self._smart_lists(1)
        pin = self._pins(1)[0]

        def abandoned_edit() -> None:
            with transaction.atomic():
                self._rename(pin, "Anything rolled back")
                raise RuntimeError("abandon the edit")

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(RuntimeError),
        ):
            abandoned_edit()

        self.assertFalse(SmartListSyncRequest.objects.exists())
        enqueue.assert_not_called()


class TheSyncAppliesEveryRequestTests(_SmartListCase):
    def _sync_inline(self):
        return tasks_run_inline(tasks.sync_requested_smart_lists)

    def test_a_saved_pin_joins_every_matching_list(self) -> None:
        pin_lists = self._smart_lists(3)
        pin = self._pins(1, name="Nothing")[0]

        with self._sync_inline(), self.captureOnCommitCallbacks(execute=True):
            self._rename(pin, "Anything now")

        for pin_list in pin_lists:
            item = PinListItem.objects.get(pin_list=pin_list, pin=pin)
            self.assertEqual(item.added_via, PinListItem.ADDED_SMART_FILTER)
        self.assertFalse(SmartListSyncRequest.objects.exists(), "the sync left its requests behind")

    def test_a_boundary_list_records_its_own_provenance(self) -> None:
        pin_list = self._smart_lists(1, smart_boundary=_SQUARE)[0]
        location = baker.make(Location, latitude=10, longitude=10)
        pin = self._pins(1)[0]

        with self._sync_inline(), self.captureOnCommitCallbacks(execute=True):
            pin.location = location
            pin.save(update_fields=["location", "updated"])

        self.assertEqual(PinListItem.objects.get(pin_list=pin_list, pin=pin).added_via, PinListItem.ADDED_BOUNDARY)

    def test_a_pin_that_stops_matching_leaves_unless_added_by_hand(self) -> None:
        # Priority, not name: a rename keeps the old name as an alias, which the name criterion still matches.
        pin_list = self._smart_lists(1, smart_filter={"min_priority": 4})[0]
        by_filter, by_hand = self._pins(2)
        PinListItem.objects.create(pin_list=pin_list, pin=by_filter, added_via=PinListItem.ADDED_SMART_FILTER)
        PinListItem.objects.create(pin_list=pin_list, pin=by_hand, added_via=PinListItem.ADDED_MANUAL)

        with self._sync_inline(), self.captureOnCommitCallbacks(execute=True):
            for pin in (by_filter, by_hand):
                pin.priority = 1
                pin.save(update_fields=["priority", "updated"])

        self.assertFalse(PinListItem.objects.filter(pin_list=pin_list, pin=by_filter).exists())
        self.assertTrue(PinListItem.objects.filter(pin_list=pin_list, pin=by_hand).exists(), "a manual add was removed")

    def test_a_bulk_edit_reaches_every_pin(self) -> None:
        pin_lists = self._smart_lists(2, smart_filter={"tags": []})
        pins = self._pins(30)
        label = baker.make(Label, profile=self.profile, name="Wanted", kind="tag")
        for pin_list in pin_lists:
            pin_list.smart_filter = {"tags": [label.pk]}
            pin_list.save(update_fields=["smart_filter"])

        with self._sync_inline(), self.captureOnCommitCallbacks(execute=True):
            bulk_edit_pins(self.profile, pins, BulkPinEdit(add_labels=[label]))

        for pin_list in pin_lists:
            self.assertEqual(set(pin_list.items.values_list("pin_id", flat=True)), {pin.pk for pin in pins})

    def test_the_sync_costs_queries_per_list_not_per_pin(self) -> None:
        self._smart_lists(3)

        def drain_cost(pin_count: int) -> int:
            pins = self._pins(pin_count)
            SmartListSyncRequest.objects.bulk_create(
                [SmartListSyncRequest(profile=self.profile, pin=pin) for pin in pins]
            )
            with CaptureQueriesContext(connection) as queries:
                smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)
            return len(queries.captured_queries)

        drain_cost(1)  # warms caches the comparison should not count
        few = drain_cost(5)
        many = drain_cost(60)

        self.assertEqual(many, few, f"60 pins cost the sync {many} queries where 5 cost {few}")

    def test_a_pin_deleted_before_the_sync_runs_is_skipped(self) -> None:
        self._smart_lists(1)
        pin = self._pins(1)[0]
        with mock.patch.object(smart_list_sync, "safely_enqueue_task"), self.captureOnCommitCallbacks(execute=True):
            self._rename(pin, "Anything gone")
        Pin.objects.filter(pk=pin.pk).delete()

        smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)

        self.assertFalse(SmartListSyncRequest.objects.exists())

    def test_a_queued_message_from_before_the_change_still_applies(self) -> None:
        """``sync_pin_against_smart_lists_task`` messages queued by the old code must not be dropped."""
        pin_list = self._smart_lists(1)[0]
        pin = self._pins(1)[0]

        tasks.sync_pin_against_smart_lists_task(pin.pk)

        self.assertTrue(PinListItem.objects.filter(pin_list=pin_list, pin=pin).exists())


class EachListIsEvaluatedOncePerPassTests(_SmartListCase):
    """Folded in from the old ``test_smart_list_sync_is_bounded.py``, which held the per-pin sync to one filter run."""

    def test_the_ceiling_is_a_real_setting(self) -> None:
        """An override_settings of a name nothing reads configures nothing."""
        from django.conf import settings

        self.assertTrue(hasattr(settings, "MAX_SMART_LISTS_PER_SYNC"))

    def test_each_filter_runs_once_for_many_pins(self) -> None:
        from urbanlens.dashboard.services.pins import pin_list_membership

        pin_lists = self._smart_lists(3)
        pins = self._pins(10)
        SmartListSyncRequest.objects.bulk_create([SmartListSyncRequest(profile=self.profile, pin=pin) for pin in pins])

        with mock.patch.object(
            pin_list_membership, "filter_matching_ids", wraps=pin_list_membership.filter_matching_ids
        ) as evaluate:
            smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)

        self.assertEqual(
            evaluate.call_count, len(pin_lists), f"3 lists over 10 pins ran a filter {evaluate.call_count} times"
        )
        for pin_list in pin_lists:
            self.assertEqual(
                pin_list.items.count(), 10, "the half that stops the count passing against a sync that does nothing"
            )


class OneSyncPerAccountAtATimeTests(_SmartListCase):
    def test_a_second_change_before_the_sync_starts_queues_nothing_more(self) -> None:
        self._smart_lists(1)
        first, second = self._pins(2)

        with mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                self._rename(first, "Anything first")
            with self.captureOnCommitCallbacks(execute=True):
                self._rename(second, "Anything second")

        self.assertEqual(enqueue.call_count, 1)

    def test_a_change_after_the_sync_starts_queues_another(self) -> None:
        """The started sync may already have read its last batch, so the next change cannot wait on it."""
        self._smart_lists(1)
        first, second = self._pins(2)

        with mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                self._rename(first, "Anything first")
            smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)
            with self.captureOnCommitCallbacks(execute=True):
                self._rename(second, "Anything second")

        self.assertEqual(enqueue.call_count, 2)

    def test_a_sync_that_finds_another_running_comes_back_later(self) -> None:
        self._smart_lists(1)
        pin = self._pins(1)[0]
        with mock.patch.object(smart_list_sync, "safely_enqueue_task"), self.captureOnCommitCallbacks(execute=True):
            self._rename(pin, "Anything waiting")

        with smart_list_sync.running_sync(self.profile.pk) as held:
            self.assertTrue(held)
            with mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue:
                smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)

        self.assertTrue(SmartListSyncRequest.objects.filter(pin=pin).exists(), "a sync ran beside another")
        enqueue.assert_called_once()
        self.assertGreater(enqueue.call_args.kwargs.get("countdown") or 0, 0)

    def test_a_request_recorded_during_a_run_outlives_it(self) -> None:
        """A run deletes the rows it read, never a row for the same pin written after."""
        pin_list = self._smart_lists(1)[0]
        pin = self._pins(1, name="Nothing")[0]
        SmartListSyncRequest.objects.create(profile=self.profile, pin=pin)
        evaluate = smart_list_sync.sync_pins_against_smart_lists

        def change_meanwhile(profile_id, pin_ids):
            evaluate(profile_id, pin_ids)
            Pin.objects.filter(pk=pin.pk).update(name="Anything after the read")
            SmartListSyncRequest.objects.create(profile=self.profile, pin=pin)

        with (
            mock.patch.object(
                smart_list_sync, "sync_pins_against_smart_lists", side_effect=change_meanwhile, autospec=True
            ),
            mock.patch.object(smart_list_sync, "safely_enqueue_task"),
        ):
            smart_list_sync.drain_smart_list_sync_requests(self.profile.pk, batch_size=1, max_batches=1)

        self.assertEqual(
            SmartListSyncRequest.objects.filter(pin=pin).count(), 1, "the newer request was deleted with the older one"
        )
        smart_list_sync.drain_smart_list_sync_requests(self.profile.pk)
        self.assertTrue(PinListItem.objects.filter(pin_list=pin_list, pin=pin).exists())

    @override_settings(MAX_SMART_LISTS_PER_SYNC=3)
    def test_an_account_past_the_ceiling_syncs_on_the_bulk_queue(self) -> None:
        self._smart_lists(4)
        pin = self._pins(1)[0]

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._rename(pin, "Anything big")

        self.assertEqual(enqueue.call_args.kwargs["queue"], tasks.Queue.BULK)

    @override_settings(MAX_SMART_LISTS_PER_SYNC=3)
    def test_an_account_within_the_ceiling_syncs_on_the_interactive_queue(self) -> None:
        self._smart_lists(3)
        pin = self._pins(1)[0]

        with (
            mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self._rename(pin, "Anything small")

        self.assertEqual(enqueue.call_args.kwargs["queue"], tasks.Queue.INTERACTIVE)


class TheSweepPicksUpLostSyncsTests(_SmartListCase):
    def test_a_stale_request_with_nothing_queued_is_queued(self) -> None:
        self._smart_lists(1)
        pin = self._pins(1)[0]
        request = SmartListSyncRequest.objects.create(profile=self.profile, pin=pin)
        SmartListSyncRequest.objects.filter(pk=request.pk).update(
            created=request.created - smart_list_sync.STALE_REQUEST_AGE * 2
        )

        with mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue:
            queued = tasks.sweep_smart_list_sync_requests()

        self.assertEqual(queued, 1)
        self.assertEqual(enqueue.call_args.args[:2], (tasks.sync_requested_smart_lists, self.profile.pk))

    def test_a_fresh_request_is_left_to_its_own_sync(self) -> None:
        self._smart_lists(1)
        pin = self._pins(1)[0]
        SmartListSyncRequest.objects.create(profile=self.profile, pin=pin)

        with mock.patch.object(smart_list_sync, "safely_enqueue_task") as enqueue:
            tasks.sweep_smart_list_sync_requests()

        enqueue.assert_not_called()


class TheListSaysWhenItIsCatchingUpTests(_SmartListCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.profile.user)
        self.smart = self._smart_lists(1)[0]
        self.plain = baker.make(PinList, profile=self.profile, is_smart=False)
        self.pin = self._pins(1)[0]

    def _pending(self) -> None:
        SmartListSyncRequest.objects.create(profile=self.profile, pin=self.pin)

    def test_the_detail_page_says_so_while_a_sync_is_owed(self) -> None:
        self._pending()

        response = self.client.get(reverse("lists.detail", args=[self.smart.slug]))

        self.assertContains(response, 'id="pin-list-sync-pending"')
        self.assertContains(response, reverse("lists.items", args=[self.smart.slug]))

    def test_the_items_panel_keeps_saying_so_until_it_settles(self) -> None:
        self._pending()
        url = reverse("lists.items", args=[self.smart.slug])

        self.assertContains(self.client.get(url), 'id="pin-list-sync-pending"')
        SmartListSyncRequest.objects.all().delete()
        self.assertNotContains(self.client.get(url), 'id="pin-list-sync-pending"')

    def test_a_settled_list_says_nothing(self) -> None:
        response = self.client.get(reverse("lists.detail", args=[self.smart.slug]))

        self.assertNotContains(response, 'id="pin-list-sync-pending"')

    def test_a_plain_list_never_says_so(self) -> None:
        self._pending()

        response = self.client.get(reverse("lists.detail", args=[self.plain.slug]))

        self.assertNotContains(response, 'id="pin-list-sync-pending"')

    def test_the_external_api_reports_it(self) -> None:
        from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        _key, raw_key = generate_api_key(self.profile.user, "Lists client")
        ApiKey.objects.filter(user=self.profile.user).update(scopes=[ApiKeyScope.LISTS_READ.value])
        url = f"/dashboard/api/external/v1/lists/{self.smart.slug}/"
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

        self.assertIs(self.client.get(url, **headers).json()["membership_pending"], False)
        self._pending()
        self.assertIs(self.client.get(url, **headers).json()["membership_pending"], True)
