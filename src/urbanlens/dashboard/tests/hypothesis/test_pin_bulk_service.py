"""The bulk pin actions are one atomic service that refits each parent's boundary once (N29 G1-9, G1-22, G1-29).

The web views looped over pins with no transaction (a merge even committed the target's promotion before finding
out it had no sources), the API copied the loops and had drifted, and every reparent refitted its parent's
child-fitted boundary: a 500-pin merge refitted one parent 500 times, reloading every child each time.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
_REFIT = "urbanlens.dashboard.services.geo.child_pin_boundaries.refit_child_pin_boundary"


class _SaveFailsFor:
    """A ``Pin.save`` stand-in that raises for one pk and saves everything else normally."""

    def __init__(self, pk: int) -> None:
        self.pk = pk
        self.original = Pin.save

    def __call__(self, pin: Pin, *args, **kwargs) -> None:
        if pin.pk == self.pk:
            raise RuntimeError("simulated failure mid-batch")
        self.original(pin, *args, **kwargs)


@override_settings(CACHES=_LOCMEM_CACHES)
class _BulkCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.client.raise_request_exception = False

    def _post(self, name: str, body: dict):
        return self.client.post(reverse(name), data=json.dumps(body), content_type="application/json")


class WebMergeAtomicityTests(_BulkCase):
    def test_a_refused_merge_does_not_keep_the_targets_promotion(self) -> None:
        parent = baker.make(Pin, profile=self.profile)
        target = baker.make(Pin, profile=self.profile, parent_pin=parent)

        response = self._post("pin.bulk_merge", {"target_uuid": str(target.uuid), "source_uuids": [str(target.uuid)]})

        self.assertEqual(response.status_code, 400)
        target.refresh_from_db()
        self.assertEqual(target.parent_pin_id, parent.pk, "the promotion was committed for a merge that never happened")

    def test_a_failure_partway_through_leaves_nothing_merged(self) -> None:
        target = baker.make(Pin, profile=self.profile)
        first, second = baker.make(Pin, profile=self.profile, _quantity=2)
        failing = max(first, second, key=lambda pin: pin.pk)

        with patch.object(Pin, "save", autospec=True, side_effect=_SaveFailsFor(failing.pk)):
            response = self._post(
                "pin.bulk_merge", {"target_uuid": str(target.uuid), "source_uuids": [str(first.uuid), str(second.uuid)]}
            )

        self.assertEqual(response.status_code, 500)
        self.assertFalse(Pin.objects.filter(parent_pin=target).exists(), "half the merge was committed")


class WebEditAtomicityTests(_BulkCase):
    def test_a_failure_partway_through_leaves_nothing_edited(self) -> None:
        first, second = baker.make(Pin, profile=self.profile, description="before", _quantity=2)
        failing = max(first, second, key=lambda pin: pin.pk)

        with patch.object(Pin, "save", autospec=True, side_effect=_SaveFailsFor(failing.pk)):
            response = self._post(
                "pin.bulk_edit", {"uuids": [str(first.uuid), str(second.uuid)], "description": "after"}
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            set(Pin.objects.filter(pk__in=[first.pk, second.pk]).values_list("description", flat=True)), {"before"}
        )


class RefitOncePerParentTests(_BulkCase):
    def test_a_web_merge_refits_the_target_once(self) -> None:
        target = baker.make(Pin, profile=self.profile)
        sources = baker.make(Pin, profile=self.profile, _quantity=6)

        with patch(_REFIT) as refit:
            response = self._post(
                "pin.bulk_merge", {"target_uuid": str(target.uuid), "source_uuids": [str(pin.uuid) for pin in sources]}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Pin.objects.filter(parent_pin=target).count(), 6)
        self.assertEqual([call.args[0] for call in refit.call_args_list], [target.pk])

    def test_an_api_reparent_refits_old_and_new_parents_once_each(self) -> None:
        from urbanlens.dashboard.services.pins.pin_bulk import BulkPinEdit, bulk_edit_pins

        old_parent, new_parent = baker.make(Pin, profile=self.profile, _quantity=2)
        children = baker.make(Pin, profile=self.profile, parent_pin=old_parent, _quantity=5)

        with patch(_REFIT) as refit:
            result = bulk_edit_pins(self.profile, children, BulkPinEdit(parent=new_parent))

        self.assertEqual(result.reparented, 5)
        self.assertEqual(sorted(call.args[0] for call in refit.call_args_list), sorted([old_parent.pk, new_parent.pk]))

    def test_a_bulk_delete_refits_each_surviving_parent_once(self) -> None:
        parent = baker.make(Pin, profile=self.profile)
        children = baker.make(Pin, profile=self.profile, parent_pin=parent, _quantity=4)

        with patch(_REFIT) as refit:
            response = self._post("pin.bulk_delete", {"uuids": [str(pin.uuid) for pin in children]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([call.args[0] for call in refit.call_args_list], [parent.pk])

    def test_nesting_root_pins_refits_the_new_parent_once(self) -> None:
        from urbanlens.dashboard.services.pins.pin_restructure import nest_root_pins

        parent = baker.make(Pin, profile=self.profile)
        roots = baker.make(Pin, profile=self.profile, _quantity=4)

        with patch(_REFIT) as refit:
            self.assertEqual(nest_root_pins(parent, roots), 4)

        self.assertEqual([call.args[0] for call in refit.call_args_list], [parent.pk])


class DeferredRefitContextTests(TestCase):
    def test_a_failed_block_refits_nothing(self) -> None:
        from urbanlens.dashboard.services.geo.child_pin_boundaries import (
            deferring_child_boundary_refits,
            request_child_boundary_refit,
        )

        with patch(_REFIT) as refit, self.assertRaises(RuntimeError), deferring_child_boundary_refits():
            request_child_boundary_refit(1)
            raise RuntimeError

        refit.assert_not_called()

    def test_nested_blocks_flush_once_at_the_outermost(self) -> None:
        from urbanlens.dashboard.services.geo.child_pin_boundaries import (
            deferring_child_boundary_refits,
            request_child_boundary_refit,
        )

        with patch(_REFIT) as refit:
            with deferring_child_boundary_refits():
                request_child_boundary_refit(2)
                with deferring_child_boundary_refits():
                    request_child_boundary_refit(2)
                    request_child_boundary_refit(1)
                refit.assert_not_called()
            self.assertEqual([call.args[0] for call in refit.call_args_list], [1, 2])

    def test_outside_a_block_it_refits_at_once(self) -> None:
        from urbanlens.dashboard.services.geo.child_pin_boundaries import request_child_boundary_refit

        with patch(_REFIT) as refit:
            request_child_boundary_refit(3)
        refit.assert_called_once_with(3)


class ApiAndWebAgreeTests(_BulkCase):
    def test_both_merges_leave_the_same_hierarchy(self) -> None:
        from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        _key, raw_key = generate_api_key(self.user, "Bulk client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value])
        for via_api in (False, True):
            with self.subTest(via_api=via_api):
                parent = baker.make(Pin, profile=self.profile)
                target = baker.make(Pin, profile=self.profile, parent_pin=parent)
                sources = baker.make(Pin, profile=self.profile, _quantity=2)
                body = {"target_uuid": str(target.uuid), "source_uuids": [str(pin.uuid) for pin in sources]}
                if via_api:
                    response = self.client.post(
                        "/dashboard/api/external/v1/pins/bulk/merge/",
                        data=json.dumps(body),
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw_key}",
                    )
                else:
                    response = self._post("pin.bulk_merge", body)
                self.assertEqual(response.status_code, 200)
                target.refresh_from_db()
                self.assertIsNone(target.parent_pin_id)
                self.assertEqual(
                    set(Pin.objects.filter(parent_pin=target).values_list("pk", flat=True)), {pin.pk for pin in sources}
                )
