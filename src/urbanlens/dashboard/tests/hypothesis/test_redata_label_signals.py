"""Integration tests for the REData label-suggestion signal wiring.

Covers ``models.labels.signals`` (taxonomy sync/retire on save/delete/reparent)
and the ``Pin.labels`` ``m2m_changed`` receiver in ``models.pin.signals``
(assignment sync). Every enqueue is deferred to ``transaction.on_commit``,
so these use Django's ``captureOnCommitCallbacks(execute=True)`` - the same
pattern already used for achievement signals - rather than calling handlers
directly, to prove the actual signal wiring (dispatch_uid, sender, m2m
reverse/forward) works end to end. Every REData HTTP call is mocked.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.UrbanLens.settings.app import settings


@contextmanager
def _redata_configured():
    with mock.patch.object(settings, "redata_api_url", "https://redata.example.test"), mock.patch.object(settings, "redata_api_key", "test-key"):
        yield


def _profile() -> Profile:
    baker.make(User)  # consumed so the "real" test profile isn't accidentally profile #1 in assertions
    user = baker.make(User)
    return Profile.objects.get(user=user)


def _pin(profile: Profile, seq: int = 1):
    offset = seq / 100
    return create_pin_for_profile(profile, name=f"Pin {seq}", latitude=40.0 + offset, longitude=-70.0 - offset).pin


def _queued_tasks(enqueue: mock.Mock) -> list:
    return [call.args[0] for call in enqueue.call_args_list if call.args]


class LabelTaxonomySignalTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_creating_a_tag_queues_definition_sync(self) -> None:
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        self.assertIn(tasks.sync_redata_label_definitions, _queued_tasks(enqueue))

    def test_creating_a_category_queues_definition_sync(self) -> None:
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            Label.objects.create(profile=self.profile, name="ZzAudit Hospital", kind=KIND_CATEGORY)
        self.assertIn(tasks.sync_redata_label_definitions, _queued_tasks(enqueue))

    def test_creating_a_status_does_not_queue_anything(self) -> None:
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            Label.objects.create(profile=self.profile, name="ZzAudit Visited", kind=KIND_STATUS)
        self.assertNotIn(tasks.sync_redata_label_definitions, _queued_tasks(enqueue))

    def test_deleting_a_tag_queues_retirement(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            label.delete()
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_label_definitions]
        self.assertTrue(calls)
        definitions = calls[-1].args[2]
        self.assertFalse(definitions[0]["is_active"])

    def test_deleting_a_status_does_not_queue_anything(self) -> None:
        label = Label.objects.create(profile=self.profile, name="ZzAudit Visited", kind=KIND_STATUS)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            label.delete()
        self.assertNotIn(tasks.sync_redata_label_definitions, _queued_tasks(enqueue))

    def test_converting_a_tag_to_status_queues_retirement(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            label.kind = KIND_STATUS
            label.save()
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_label_definitions]
        self.assertTrue(calls)
        definitions = calls[-1].args[2]
        self.assertFalse(definitions[0]["is_active"])

    def test_converting_a_status_to_category_queues_an_active_definition(self) -> None:
        label = ensure_label(profile=self.profile, name="Was Status", kind=KIND_STATUS)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            label.kind = KIND_CATEGORY
            label.save()
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_label_definitions]
        self.assertTrue(calls)
        definitions = calls[-1].args[2]
        self.assertTrue(definitions[0]["is_active"])

    def test_reparenting_queues_the_childs_definition(self) -> None:
        parent = Label.objects.create(profile=self.profile, name="ZzAudit Hospital", kind=KIND_CATEGORY)
        # A fresh name, not a seeded default: "Asylum" ships with a parent in the
        # default taxonomy, so reusing it would leave the child with two parents
        # and the assertion below would see both.
        child = Label.objects.create(profile=self.profile, name="ZzAudit Asylum", kind=KIND_CATEGORY)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            child.parents.add(parent)
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_label_definitions]
        self.assertTrue(calls)
        definitions = calls[-1].args[2]
        self.assertEqual(definitions[0]["external_id"], str(child.uuid))
        self.assertEqual(definitions[0]["parent_ids"], [str(parent.uuid)])


class PinLabelAssignmentSignalTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_adding_a_tag_to_a_pin_queues_assignment_sync(self) -> None:
        pin = _pin(self.profile, seq=1)
        tag = ensure_label(profile=self.profile, name="Notable", kind=KIND_TAG)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            pin.labels.add(tag)
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_pin_assignment]
        self.assertTrue(calls)
        self.assertEqual(calls[-1].args[1], pin.pk)

    def test_removing_a_tag_from_a_pin_queues_assignment_sync(self) -> None:
        pin = _pin(self.profile, seq=1)
        tag = ensure_label(profile=self.profile, name="Notable", kind=KIND_TAG)
        pin.labels.add(tag)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            pin.labels.remove(tag)
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_pin_assignment]
        self.assertTrue(calls)

    def test_reverse_add_from_the_label_side_queues_assignment_sync(self) -> None:
        """Mirrors services.labels.merge's ``target.pins.add(*source.pins.all())``."""
        pin = _pin(self.profile, seq=1)
        tag = ensure_label(profile=self.profile, name="Notable", kind=KIND_TAG)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            tag.pins.add(pin)
        calls = [call for call in enqueue.call_args_list if call.args and call.args[0] is tasks.sync_redata_pin_assignment]
        self.assertTrue(calls)
        self.assertEqual(calls[-1].args[1], pin.pk)

    def test_reverse_add_of_a_status_label_does_not_queue_anything(self) -> None:
        pin = _pin(self.profile, seq=1)
        status = Label.objects.create(profile=self.profile, name="ZzAudit Visited", kind=KIND_STATUS)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            status.pins.add(pin)
        self.assertNotIn(tasks.sync_redata_pin_assignment, _queued_tasks(enqueue))
