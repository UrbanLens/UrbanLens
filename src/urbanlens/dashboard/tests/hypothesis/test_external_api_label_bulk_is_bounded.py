"""The external label bulk edit caps the labels but not the parents.

N21 H33, and the external twin of H26 - which was capped at the internal door
in an earlier batch, leaving this one open. `LabelBulkEditSerializer` gives
`uuids` a `max_length=500`, and gives `add_parent_uuids` and `add_child_uuids`
no ceiling at all. The work is the product of the two: for every label, for
every proposed parent, `would_create_cycle` walks the label graph in the
database - so one capped number multiplied by an uncapped one, inside a single
`transaction.atomic()` holding its rows the whole time.

Refused rather than trimmed, matching the internal door and for the same
reason: a bulk edit that silently applied to part of what was selected is worse
than one that says no. `max_length` on the field means DRF refuses it in
validation, before a transaction is opened or a single graph walk runs.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_BASE = "/dashboard/api/external/v1/labels/"
CEILING_SETTING = "LABEL_BULK_EDIT_MAX_IDS"


class _BulkEditCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Label bulk client")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[ApiKeyScope.LABELS_READ.value, ApiKeyScope.LABELS_WRITE.value]
        )
        self.label = ensure_label(profile=self.profile, name="Rusty", kind=KIND_TAG)

    def _post(self, payload: dict):
        return self.client.post(
            f"{_BASE}bulk/edit/",
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
        )

    def _uuids(self, count: int) -> list[str]:
        return [
            str(ensure_label(profile=self.profile, name=f"Parent {index}", kind=KIND_TAG).uuid)
            for index in range(count)
        ]


class TheSettingExistsTests(_BulkEditCase):
    def test_the_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, CEILING_SETTING), f"nothing reads {CEILING_SETTING}")
        self.assertGreater(getattr(settings, CEILING_SETTING), 0)


@override_settings(**{CEILING_SETTING: 3})
class TheParentListsAreBoundedTests(_BulkEditCase):
    """The uncapped half of a product whose other half was already capped.

    Every list here names **real, resolvable** labels. The view already refuses
    a uuid it cannot resolve, so a list of invented uuids would be refused for
    that reason and the test would pass against an uncapped endpoint - which is
    exactly what the first draft of this file did.
    """

    def test_too_many_parents_is_refused(self) -> None:
        response = self._post({"uuids": [str(self.label.uuid)], "add_parent_uuids": self._uuids(4)})

        self.assertEqual(response.status_code, 400, response.content[:200])

    def test_too_many_children_is_refused(self) -> None:
        """Both lists feed the same O(labels x proposed) walk, so both need the ceiling."""
        response = self._post({"uuids": [str(self.label.uuid)], "add_child_uuids": self._uuids(4)})

        self.assertEqual(response.status_code, 400, response.content[:200])

    def test_too_many_labels_is_still_refused(self) -> None:
        """The half that was already capped keeps its ceiling when the rule moves."""
        response = self._post({"uuids": self._uuids(4), "icon": "star"})

        self.assertEqual(response.status_code, 400, response.content[:200])

    def test_the_refusal_happens_before_any_write(self) -> None:
        """A validation refusal, not a half-applied edit that ran out of room."""
        response = self._post({"uuids": [str(self.label.uuid)], "icon": "star", "add_parent_uuids": self._uuids(4)})

        self.assertEqual(response.status_code, 400, response.content[:200])
        self.label.refresh_from_db()
        self.assertNotEqual(self.label.icon, "star", "the edit was applied despite the refusal")


@override_settings(**{CEILING_SETTING: 3})
class OrdinaryEditsStillWorkTests(_BulkEditCase):
    """The half that stops the ceiling above passing against an endpoint that refuses everything."""

    def test_a_parent_list_at_the_ceiling_is_accepted(self) -> None:
        response = self._post({"uuids": [str(self.label.uuid)], "add_parent_uuids": self._uuids(3)})

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(self.label.parents.count(), 3)

    def test_a_plain_field_edit_still_applies(self) -> None:
        response = self._post({"uuids": [str(self.label.uuid)], "icon": "star"})

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.label.refresh_from_db()
        self.assertEqual(self.label.icon, "star")

    def test_an_unresolvable_parent_is_still_refused_on_its_own_terms(self) -> None:
        """The pre-existing 400 must not be what the ceiling tests are measuring."""
        response = self._post(
            {"uuids": [str(self.label.uuid)], "add_parent_uuids": [str(baker.prepare("dashboard.Label").uuid)]}
        )

        self.assertEqual(response.status_code, 400, response.content[:200])
