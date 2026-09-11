"""Saving one pin must not cost a filter evaluation per smart list its owner owns.

Every ``Pin.save()`` - and every label change, which is a separate receiver -
re-evaluates the pin against every active smart list the profile has, each
evaluation deserialising the list's criteria and running its own query, with no
ceiling on how many smart lists a profile may create (N21 H27). A bulk edit
multiplies the two together inside one request.

Nothing is dropped: past the ceiling the whole sync moves to the bulk queue, so
a profile with hundreds of smart lists still gets all of them applied, just not
while a request and a database connection are held open. The matching pin is
also evaluated once rather than twice - deciding membership and deciding which
rule to record were two separate passes over the same filter.
"""

from __future__ import annotations

from unittest import mock

from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins import pin_list_membership

SETTING_NAME = "MAX_SMART_LISTS_PER_SYNC"


class _SmartListCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _smart_lists(self, count: int) -> list[PinList]:
        """Lists whose filter matches anything, so membership is never the variable."""
        return [
            baker.make(PinList, profile=self.profile, is_smart=True, smart_filter={"name": ""}) for _ in range(count)
        ]

    def _save_a_pin(self) -> Pin:
        with self.captureOnCommitCallbacks(execute=True):
            return baker.make(Pin, profile=self.profile, name="Anything")


class TheSettingExistsTests(_SmartListCase):
    def test_the_ceiling_is_a_real_setting(self) -> None:
        """An override_settings of a name nothing reads configures nothing."""
        from django.conf import settings

        self.assertTrue(hasattr(settings, SETTING_NAME), f"nothing reads {SETTING_NAME}")


class PastTheCeilingTheWorkLeavesTheRequestTests(_SmartListCase):
    @override_settings(**{SETTING_NAME: 3})
    def test_a_profile_over_the_ceiling_hands_the_sync_off(self) -> None:
        self._smart_lists(6)

        with mock.patch.object(pin_list_membership, "safely_enqueue_task") as enqueue:
            pin = self._save_a_pin()

        self.assertTrue(enqueue.called, "six smart lists were evaluated inside the request")
        self.assertIn(pin.pk, enqueue.call_args.args)

    @override_settings(**{SETTING_NAME: 3})
    def test_a_profile_under_the_ceiling_is_still_done_inline(self) -> None:
        """The half that stops the test above passing against a sync that always defers."""
        lists = self._smart_lists(2)

        with mock.patch.object(pin_list_membership, "safely_enqueue_task") as enqueue:
            pin = self._save_a_pin()

        enqueue.assert_not_called()
        for pin_list in lists:
            self.assertTrue(PinListItem.objects.filter(pin_list=pin_list, pin=pin).exists())

    @override_settings(**{SETTING_NAME: 3, "CELERY_TASK_ALWAYS_EAGER": True, "CELERY_TASK_EAGER_PROPAGATES": True})
    def test_nothing_is_dropped_when_the_work_is_deferred(self) -> None:
        lists = self._smart_lists(6)

        pin = self._save_a_pin()

        for pin_list in lists:
            self.assertTrue(
                PinListItem.objects.filter(pin_list=pin_list, pin=pin).exists(),
                f"list {pin_list.pk} never received the pin after the sync was deferred",
            )


class TheFilterIsEvaluatedOncePerListTests(_SmartListCase):
    @override_settings(**{SETTING_NAME: 50})
    def test_a_matching_pin_does_not_run_its_filter_twice(self) -> None:
        self._smart_lists(1)

        with mock.patch.object(
            pin_list_membership, "_pin_matches_filter", wraps=pin_list_membership._pin_matches_filter
        ) as evaluate:
            self._save_a_pin()

        self.assertEqual(
            evaluate.call_count,
            1,
            f"one pin against one smart list ran the filter {evaluate.call_count} times",
        )

    @override_settings(**{SETTING_NAME: 50})
    def test_the_provenance_is_still_recorded(self) -> None:
        """The half that stops the test above passing against a sync that stopped recording it."""
        pin_list = self._smart_lists(1)[0]

        pin = self._save_a_pin()

        item = PinListItem.objects.get(pin_list=pin_list, pin=pin)
        self.assertEqual(item.added_via, PinListItem.ADDED_SMART_FILTER)
