"""Tests for services.labels.redata_suggestions - syncing tag/category labels to REData.

Covers the definition/assignment payload builders, the profile-fanout for
global vs owned labels, the queue_* helpers' REData-not-configured no-ops,
sync_label_definitions/sync_pin_assignment/backfill_profile, and
get_suggestions. Every REData HTTP call is mocked - never hits the network.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.labels import redata_suggestions
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.UrbanLens.settings.app import settings


@contextmanager
def _redata_configured():
    with mock.patch.object(settings, "redata_api_url", "https://redata.example.test"), mock.patch.object(settings, "redata_api_key", "test-key"):
        yield


@contextmanager
def _redata_not_configured():
    with mock.patch.object(settings, "redata_api_url", None), mock.patch.object(settings, "redata_api_key", None):
        yield


def _profile() -> Profile:
    baker.make(User)  # consumed so the "real" test profile isn't accidentally profile #1 in assertions
    user = baker.make(User)
    return Profile.objects.get(user=user)


def _pin(profile: Profile, name: str | None = None, seq: int = 1):
    offset = seq / 100
    return create_pin_for_profile(profile, name=name, latitude=40.0 + offset, longitude=-70.0 - offset).pin


class LabelDefinitionTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_builds_external_id_name_and_parent_ids(self) -> None:
        parent = ensure_label(profile=self.profile, name="Hospital", kind=KIND_CATEGORY)
        label = ensure_label(profile=self.profile, name="Asylum", kind=KIND_CATEGORY, description="Psych hospitals")
        label.parents.add(parent)
        definition = redata_suggestions._label_definition(label, is_active=True)
        self.assertEqual(definition["external_id"], str(label.uuid))
        self.assertEqual(definition["name"], "Asylum")
        self.assertEqual(definition["parent_ids"], [str(parent.uuid)])
        self.assertEqual(definition["description"], "Psych hospitals")
        self.assertTrue(definition["is_active"])

    def test_non_suggestable_parents_are_excluded(self) -> None:
        status_parent = ensure_label(profile=self.profile, name="Visited", kind=KIND_STATUS)
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        label.parents.add(status_parent)
        definition = redata_suggestions._label_definition(label, is_active=True)
        self.assertEqual(definition["parent_ids"], [])

    def test_is_active_false_for_retirement(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        definition = redata_suggestions._label_definition(label, is_active=False)
        self.assertFalse(definition["is_active"])


class ProfileIdsForLabelTests(TestCase):
    def test_owned_label_maps_to_its_one_owner(self) -> None:
        profile = _profile()
        label = ensure_label(profile=profile, name="Church", kind=KIND_TAG)
        self.assertEqual(redata_suggestions._profile_ids_for_label(label), [profile.pk])

    def test_global_label_maps_to_every_profile(self) -> None:
        profile_a = _profile()
        profile_b = _profile()
        label = ensure_label(profile=None, name="Church", kind=KIND_CATEGORY)
        profile_ids = redata_suggestions._profile_ids_for_label(label)
        self.assertIn(profile_a.pk, profile_ids)
        self.assertIn(profile_b.pk, profile_ids)


class PinAssignmentEntryTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_includes_only_suggestable_label_ids(self) -> None:
        pin = _pin(self.profile, seq=1)
        tag = ensure_label(profile=self.profile, name="Notable", kind=KIND_TAG)
        status = ensure_label(profile=self.profile, name="Visited", kind=KIND_STATUS)
        pin.labels.add(tag, status)
        entry = redata_suggestions._pin_assignment_entry(pin)
        self.assertEqual(entry["external_id"], str(pin.uuid))
        self.assertEqual(entry["label_ids"], [str(tag.uuid)])
        self.assertTrue(entry["replace"])

    def test_names_included_only_when_pin_has_a_name(self) -> None:
        named = _pin(self.profile, name="Old Mill", seq=2)
        unnamed = _pin(self.profile, name=None, seq=3)
        self.assertEqual(redata_suggestions._pin_assignment_entry(named)["names"], ["Old Mill"])
        self.assertNotIn("names", redata_suggestions._pin_assignment_entry(unnamed))


class SyncLabelDefinitionsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_not_configured_does_nothing(self) -> None:
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway") as gw:
            redata_suggestions.sync_label_definitions([self.profile.pk], [{"external_id": "abc", "name": "Church"}])
        gw.assert_not_called()

    def test_calls_gateway_once_per_profile(self) -> None:
        other = _profile()
        gateway_instance = mock.Mock()
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            redata_suggestions.sync_label_definitions([self.profile.pk, other.pk], [{"external_id": "abc", "name": "Church"}])
        self.assertEqual(gateway_instance.define_labels.call_count, 2)

    def test_one_profile_failure_does_not_block_the_next(self) -> None:
        other = _profile()
        gateway_instance = mock.Mock()
        gateway_instance.define_labels.side_effect = [GatewayRequestError("boom"), {"created": 1}]
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            redata_suggestions.sync_label_definitions([self.profile.pk, other.pk], [{"external_id": "abc", "name": "Church"}])  # must not raise
        self.assertEqual(gateway_instance.define_labels.call_count, 2)


class SyncPinAssignmentTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_not_configured_does_nothing(self) -> None:
        pin = _pin(self.profile, seq=1)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway") as gw:
            redata_suggestions.sync_pin_assignment(pin)
        gw.assert_not_called()

    def test_configured_calls_gateway_with_current_labels(self) -> None:
        pin = _pin(self.profile, seq=1)
        tag = ensure_label(profile=self.profile, name="Notable", kind=KIND_TAG)
        pin.labels.add(tag)
        gateway_instance = mock.Mock()
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            redata_suggestions.sync_pin_assignment(pin)
        gateway_instance.sync_assignments.assert_called_once()
        entries = gateway_instance.sync_assignments.call_args.args[1]
        self.assertEqual(entries[0]["label_ids"], [str(tag.uuid)])

    def test_gateway_failure_is_swallowed(self) -> None:
        pin = _pin(self.profile, seq=1)
        gateway_instance = mock.Mock()
        gateway_instance.sync_assignments.side_effect = GatewayRequestError("boom")
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            redata_suggestions.sync_pin_assignment(pin)  # must not raise


class QueueLabelDefinitionSyncTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_non_suggestable_kind_does_not_schedule_on_commit(self) -> None:
        label = ensure_label(profile=self.profile, name="Visited", kind=KIND_STATUS)
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit") as on_commit:
            redata_suggestions.queue_label_definition_sync(label)
        on_commit.assert_not_called()

    def test_not_configured_does_not_schedule_on_commit(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit") as on_commit:
            redata_suggestions.queue_label_definition_sync(label)
        on_commit.assert_not_called()

    def test_suggestable_kind_enqueues_after_commit(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        callbacks: list = []
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            redata_suggestions.queue_label_definition_sync(label)
            callbacks[0]()
        enqueue.assert_called_once()


class QueueLabelRetirementTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_suggestable_kind_enqueues_a_retired_definition_after_commit(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        callbacks: list = []
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            redata_suggestions.queue_label_retirement(label)
            callbacks[0]()
        enqueue.assert_called_once()
        definitions = enqueue.call_args.args[2]
        self.assertFalse(definitions[0]["is_active"])

    def test_not_configured_does_not_schedule_on_commit(self) -> None:
        label = ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit") as on_commit:
            redata_suggestions.queue_label_retirement(label)
        on_commit.assert_not_called()

    def test_schedules_regardless_of_the_labels_current_kind(self) -> None:
        """A kind-change-away caller passes a label whose *current* kind is
        already the new (non-suggestable) one - retirement must not re-check
        it, since callers already decided based on the kind it *was*."""
        label = ensure_label(profile=self.profile, name="Was Tag", kind=KIND_STATUS)
        callbacks: list = []
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            redata_suggestions.queue_label_retirement(label)
            callbacks[0]()
        enqueue.assert_called_once()


class QueuePinAssignmentSyncTests(TestCase):
    def test_not_configured_does_not_schedule_on_commit(self) -> None:
        with _redata_not_configured(), mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit") as on_commit:
            redata_suggestions.queue_pin_assignment_sync(1)
        on_commit.assert_not_called()

    def test_configured_enqueues_with_pin_id_after_commit(self) -> None:
        callbacks: list = []
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.labels.redata_suggestions.transaction.on_commit", side_effect=callbacks.append),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            redata_suggestions.queue_pin_assignment_sync(42)
            callbacks[0]()
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], 42)


# These exercise the production send path. Off production the write is skipped by design
# (see services.core.environment), and the test environment is not production - so the
# send path has to be opted into explicitly here.
@override_settings(IS_PRODUCTION=True)
class BackfillProfileTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_not_configured_returns_zero_counts(self) -> None:
        with _redata_not_configured():
            result = redata_suggestions.backfill_profile(self.profile)
        self.assertEqual(result, (0, 0))

    def test_syncs_labels_and_pins(self) -> None:
        # New profiles auto-seed a default tag/category taxonomy (see
        # models.labels.signals.create_default_tags) - count against that
        # baseline rather than assuming an empty taxonomy.
        baseline = Label.objects.visible_to(self.profile).suggestable().count()
        ensure_label(profile=self.profile, name="Church", kind=KIND_TAG)
        ensure_label(profile=self.profile, name="New Status", kind=KIND_STATUS)
        _pin(self.profile, seq=1)
        gateway_instance = mock.Mock()
        with (
            _redata_configured(),
            mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance),
        ):
            labels_synced, pins_synced = redata_suggestions.backfill_profile(self.profile)
        self.assertEqual(labels_synced, baseline + 1)  # the new tag, not the new status label
        self.assertEqual(pins_synced, 1)
        gateway_instance.sync_assignments.assert_called_once()


class GetSuggestionsTests(TestCase):
    def setUp(self) -> None:
        self.profile = _profile()

    def test_not_configured_returns_none(self) -> None:
        pin = _pin(self.profile, seq=1)
        with _redata_not_configured():
            self.assertIsNone(redata_suggestions.get_suggestions(pin))

    def test_gateway_failure_returns_none(self) -> None:
        pin = _pin(self.profile, seq=1)
        gateway_instance = mock.Mock()
        gateway_instance.suggest_labels.side_effect = GatewayRequestError("boom")
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            self.assertIsNone(redata_suggestions.get_suggestions(pin))

    def test_resolves_results_to_local_labels_and_drops_unknown_ids(self) -> None:
        pin = _pin(self.profile, seq=1)
        label = ensure_label(profile=self.profile, name="Lighthouse", kind=KIND_TAG)
        gateway_instance = mock.Mock()
        gateway_instance.suggest_labels.return_value = {
            "results": [
                {"label_id": str(label.uuid), "name": "Lighthouse", "confidence": 0.82},
                {"label_id": "unknown-uuid", "name": "Ghost", "confidence": 0.5},
            ],
        }
        with _redata_configured(), mock.patch("urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway", return_value=gateway_instance):
            suggestions = redata_suggestions.get_suggestions(pin)
        assert suggestions is not None
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0][0].pk, label.pk)
        self.assertAlmostEqual(suggestions[0][1], 0.82)


class RedataLabelsConfiguredHelperTests(SimpleTestCase):
    def test_true_only_when_both_url_and_key_set(self) -> None:
        with _redata_configured():
            self.assertTrue(redata_suggestions.redata_labels_configured())
        with _redata_not_configured():
            self.assertFalse(redata_suggestions.redata_labels_configured())
