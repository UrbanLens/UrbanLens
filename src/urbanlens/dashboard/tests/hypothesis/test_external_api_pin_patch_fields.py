"""Tests for the *widened* ``PATCH /pins/{slug}/`` payload.

``PinUpdateSerializer`` originally accepted only name/icon/last_visited/
coordinates/parent_id and **silently dropped** everything else while still
answering 200 - so a user who edited a pin's description in the mobile app saw
a success and lost the edit. Widening it to cover the whole of what the
website's own pin-detail dialog can change is what these tests cover.
``test_external_api_pin_detail.py`` still owns the original narrow surface
(rename, re-icon, coordinate move, parent detach/reparent, DELETE); this module
picks up where that one stops.

Four things here are not "just another field" and get their own class:

* **Partial semantics.** Absent means untouched, an explicit null clears. That
  distinction is the entire point of the serializer, and getting it wrong in
  either direction destroys data the client never mentioned.
* **``label_uuids`` is a full replacement, and each removal is tombstoned.**
  Without the ``PinAutoRemoval`` row the removal does not stick: keyword and AI
  auto-tagging re-derive labels from the pin's own text and would put the label
  straight back, so the user would watch a label they just deleted reappear.
* **``priority``/``danger``/``vulnerability`` are not private edits.** Writing
  one publishes the owner's community ``WikiStatVote`` on the attached wiki,
  where everyone with access to that wiki sees it feed the composite score.
  A surprising consequence of a seemingly private edit deserves a test that
  says so out loud.
* **The nested ``security`` object collides with a ``Pin`` column of the same
  name**, which used to turn ``{"security": {"locked": "everywhere"}}`` into a
  500. See :class:`PinUpdateEditMappingTests`.
"""

from __future__ import annotations

import datetime
from typing import Any
from uuid import uuid4

from django.contrib.auth.models import User
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.external_api.serializers import PinUpdateSerializer
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pin_edit import EDITABLE_PIN_FIELDS, SECURITY_EDIT_FIELDS

BASE = "/dashboard/api/external/v1/pins"

#: A key issued for this API carries the default scopes; these tests want both
#: halves of the pin surface, and narrow deliberately in the denial tests.
_PIN_SCOPES = [ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value]

#: One valid value per newly-writable flat ``Pin`` field, used both as a
#: worked example and as the alphabet for the property test below. Values are
#: in *wire* form (what a client would put in the JSON body).
_SAMPLE_FIELD_VALUES: dict[str, Any] = {
    "name": "Old Mill",
    "icon": "factory",
    "description": "Rusted catwalks - watch the floor.",
    "color": "#F44336",
    "pin_type": PinType.BUILDING.value,
    "priority": 4,
    "danger": 3,
    "vulnerability": 2,
    "date_built": "1902-05-01",
    "date_abandoned": "1988-11-30",
    "date_last_active": "1987-01-15",
}


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _PinPatchTestCase(TestCase):
    """Shared fixture: a key owner holding pins:read + pins:write, and one pin."""

    def setUp(self) -> None:
        """Create the key owner, a bystander, a pin-scoped key, and a pin."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_profile = Profile.objects.get(user=baker.make(User, username="bystander"))
        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=_PIN_SCOPES)
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def _url(self, pin: Pin | None = None) -> str:
        """The detail URL of *pin* (default: the fixture pin).

        Args:
            pin: The pin to address.

        Returns:
            The fully-built URL.
        """
        pin = pin or self.pin
        return f"{BASE}/{pin.slug or pin.uuid}/"

    def _patch(self, payload: dict, *, pin: Pin | None = None, raw_key: str | None = None):
        """PATCH a pin with a JSON body.

        Args:
            payload: The JSON body to send.
            pin: The pin to address; defaults to the fixture pin.
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            The Django test-client response.
        """
        return self.client.patch(self._url(pin), payload, content_type="application/json", **_bearer(raw_key or self.raw_key))

    def _get(self, pin: Pin | None = None):
        """GET a pin's detail payload.

        Args:
            pin: The pin to address; defaults to the fixture pin.

        Returns:
            The Django test-client response.
        """
        return self.client.get(self._url(pin), **_bearer(self.raw_key))

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes*.

        Args:
            scopes: Raw scope values to store on the row.

        Returns:
            The raw key value.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _label(self, name: str, kind: str = KIND_TAG, profile: Profile | None = None) -> Label:
        """Create one label, owned by the key holder unless told otherwise.

        Args:
            name: The label's name.
            kind: One of the ``KIND_*`` constants.
            profile: The owning profile; ``None`` means a *global* label.

        Returns:
            The created label.
        """
        return Label.objects.create(name=name, kind=kind, profile=profile if profile is not None else self.profile)


class PinPatchRoundTripTests(_PinPatchTestCase):
    """Every newly-writable field is readable back on the next GET.

    The failure this guards is the exact one the widening exists to fix: a
    field the endpoint accepts, answers 200 for, and then quietly does not
    store. Asserting through GET rather than the ORM is deliberate - the pin's
    owner reads it back through the API, so that is where it has to be true.
    """

    def test_description_round_trips(self) -> None:
        response = self._patch({"description": "Rusted catwalks - watch the floor."})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["description"], "Rusted catwalks - watch the floor.")

    def test_color_round_trips(self) -> None:
        """Asserted on ``own_color``: ``color`` is the *effective* colour, which
        falls back to a label's when the pin has none of its own."""
        response = self._patch({"color": "#F44336"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["own_color"], "#F44336")

    def test_icon_round_trips(self) -> None:
        response = self._patch({"icon": "factory"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["own_icon"], "factory")

    def test_priority_round_trips(self) -> None:
        response = self._patch({"priority": 4})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["priority"], 4)

    def test_pin_type_round_trips_and_marks_the_type_user_provided(self) -> None:
        """The companion flag matters as much as the value.

        Without it, automatic building/parcel classification
        (``services.locations.site_scope``) would go on overruling the type the
        user deliberately chose, and the choice would appear to "not stick".
        """
        response = self._patch({"pin_type": PinType.BUILDING.value})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["pin_type"], PinType.BUILDING.value)
        self.pin.refresh_from_db()
        self.assertTrue(self.pin.pin_type_is_user_provided)

    def test_resubmitting_the_type_it_already_had_still_marks_it_user_provided(self) -> None:
        """The user looked at the control and confirmed it - that is a choice."""
        self.assertFalse(self.pin.pin_type_is_user_provided)

        response = self._patch({"pin_type": self.pin.pin_type})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertTrue(self.pin.pin_type_is_user_provided)

    def test_the_three_abandonment_dates_round_trip(self) -> None:
        response = self._patch({"date_built": "1902-05-01", "date_abandoned": "1988-11-30", "date_last_active": "1987-01-15"})

        self.assertEqual(response.status_code, 200, response.content)
        body = self._get().json()
        self.assertEqual(body["date_built"], "1902-05-01")
        self.assertEqual(body["date_abandoned"], "1988-11-30")
        self.assertEqual(body["date_last_active"], "1987-01-15")

    def test_last_visited_round_trips(self) -> None:
        response = self._patch({"last_visited": "2024-06-01T12:00:00Z"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("2024-06-01", self._get().json()["last_visited"])

    def test_all_eight_security_indicators_round_trip(self) -> None:
        """Includes the indicator literally named ``security``.

        That one shares its name with the wire key carrying the whole nested
        object, which used to make this payload a 500 - see
        :class:`PinUpdateEditMappingTests`.
        """
        submitted = dict.fromkeys(sorted(SECURITY_EDIT_FIELDS), SecurityLevel.EVERYWHERE.value)

        response = self._patch({"security": submitted})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["security"], submitted)

    def test_a_single_security_indicator_leaves_the_other_seven_alone(self) -> None:
        """A client that only learned the gate is locked restates nothing else."""
        response = self._patch({"security": {"locked": SecurityLevel.EVERYWHERE.value}})

        self.assertEqual(response.status_code, 200, response.content)
        security = self._get().json()["security"]
        self.assertEqual(security["locked"], SecurityLevel.EVERYWHERE.value)
        for name in sorted(SECURITY_EDIT_FIELDS - {"locked"}):
            with self.subTest(indicator=name):
                self.assertEqual(security[name], SecurityLevel.UNKNOWN.value)

    def test_an_unknown_security_level_is_rejected(self) -> None:
        response = self._patch({"security": {"locked": "sometimes"}})

        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.locked, SecurityLevel.UNKNOWN.value)

    def test_danger_and_vulnerability_are_stored_but_not_yet_served_back(self) -> None:
        """KNOWN GAP, asserted so it cannot be mistaken for working.

        ``PATCH`` accepts both, and both are persisted - but neither appears in
        the pin-detail payload, because ``services.map_pins.payload`` (which
        ``services.pin_detail.build_pin_detail`` builds on) never emitted them
        and the schema serializers honestly reflect that. The result is two
        write-only fields: a client cannot read back what it just wrote, so it
        has no way to detect a lost write or reconcile after an offline edit.

        Fixing it means teaching ``build_pin_detail`` to emit them (and
        widening ``PinDetailSerializer`` to match, or
        ``test_external_api_schema.PinDetailContractTests`` will fail) - files
        outside this change's ownership. When that lands, this test should be
        flipped into an ordinary round-trip assertion.
        """
        response = self._patch({"danger": 3, "vulnerability": 2})
        self.assertEqual(response.status_code, 200, response.content)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.danger, 3)
        self.assertEqual(self.pin.vulnerability, 2)

        body = self._get().json()
        self.assertNotIn("danger", body)
        self.assertNotIn("vulnerability", body)

    def test_the_patch_response_is_the_same_document_the_next_get_returns(self) -> None:
        """Saves the client a round trip, and only if the two really agree."""
        patched = self._patch({"description": "Boiler house", "priority": 2, "color": "#0088CC"})

        self.assertEqual(patched.status_code, 200, patched.content)
        fetched = self._get()
        # `updated` moves on every write and the boundary is regenerated lazily,
        # so compare the fields this request actually set rather than the whole
        # document - a mismatch there is what would mislead a client.
        for field in ("description", "priority", "own_color"):
            with self.subTest(field=field):
                self.assertEqual(patched.json()[field], fetched.json()[field])


class PinPatchPartialSemanticsTests(_PinPatchTestCase):
    """Absent means untouched; an explicit null clears.

    Both halves have to hold or a client destroys data it never mentioned: a
    single-control quick edit that rewrites every field silently reverts
    whatever another device changed since, and a "clear this note" that no-ops
    leaves the user staring at text they deleted.
    """

    def _populate(self) -> None:
        """Give the fixture pin a value in every nullable field."""
        response = self._patch(
            {
                "description": "Rusted catwalks.",
                "color": "#F44336",
                "icon": "factory",
                "priority": 4,
                "danger": 3,
                "vulnerability": 2,
                "date_built": "1902-05-01",
                "date_abandoned": "1988-11-30",
                "date_last_active": "1987-01-15",
                "last_visited": "2024-06-01T12:00:00Z",
                "security": {"locked": SecurityLevel.EVERYWHERE.value},
            }
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_a_one_field_patch_leaves_every_other_field_untouched(self) -> None:
        self._populate()
        self.pin.refresh_from_db()
        before = {name: getattr(self.pin, name) for name in sorted(EDITABLE_PIN_FIELDS) if name != "priority"}

        response = self._patch({"priority": 1})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.priority, 1)
        for name, value in before.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(self.pin, name), value)

    def test_explicit_null_clears_the_free_text_fields(self) -> None:
        self._populate()

        response = self._patch({"description": None, "color": None, "icon": None})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.description)
        self.assertIsNone(self.pin.color)
        self.assertIsNone(self.pin.icon)

    def test_a_blank_string_clears_a_free_text_field_exactly_like_null(self) -> None:
        """"Cleared in the UI" and "explicit JSON null" must land on one stored value.

        Otherwise a pin edited on the website holds ``""`` where the same edit
        from the mobile app holds ``NULL``, and every ``field__isnull`` filter
        disagrees about which pins have a description.
        """
        self._populate()

        response = self._patch({"description": "   "})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.description)

    def test_explicit_null_clears_the_date_fields(self) -> None:
        self._populate()

        response = self._patch({"date_built": None, "date_abandoned": None, "date_last_active": None, "last_visited": None})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.date_built)
        self.assertIsNone(self.pin.date_abandoned)
        self.assertIsNone(self.pin.date_last_active)
        self.assertIsNone(self.pin.last_visited)

    def test_null_is_refused_for_the_fields_that_have_no_null_state(self) -> None:
        """``priority``/``danger``/``vulnerability`` use 0 for "unset" and
        ``pin_type``/the security indicators have their own "unknown" member,
        so the columns are non-nullable. Refusing null is better than inventing
        a mapping the client did not ask for."""
        self._populate()

        for payload in ({"priority": None}, {"danger": None}, {"vulnerability": None}, {"pin_type": None}, {"security": {"locked": None}}):
            with self.subTest(payload=payload):
                response = self._patch(payload)
                self.assertEqual(response.status_code, 400, response.content)

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.priority, 4)
        self.assertEqual(self.pin.locked, SecurityLevel.EVERYWHERE.value)

    def test_an_empty_patch_body_changes_nothing_and_still_returns_the_pin(self) -> None:
        self._populate()
        self.pin.refresh_from_db()
        before = {name: getattr(self.pin, name) for name in sorted(EDITABLE_PIN_FIELDS)}

        response = self._patch({})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        for name, value in before.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(self.pin, name), value)

    def test_a_rejected_field_writes_none_of_the_others_in_the_same_patch(self) -> None:
        """Validation happens before anything is written - a partly-applied
        PATCH is the worst outcome, because the client cannot tell which half
        landed."""
        response = self._patch({"description": "should not land", "priority": 99})

        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.description, "should not land")


class PinPatchLabelReplacementTests(_PinPatchTestCase):
    """``label_uuids`` replaces the pin's organize labels and tombstones removals."""

    def setUp(self) -> None:
        """Add three of the owner's own labels and attach two to the pin."""
        super().setUp()
        # Auto-tagging may have attached labels at creation; start from a known
        # set so "full replacement" is asserted against something exact.
        self.pin.labels.clear()
        self.tag = self._label("rooftop", KIND_TAG)
        self.category = self._label("factory", KIND_CATEGORY)
        self.status = self._label("scouted", KIND_STATUS)
        self.pin.labels.add(self.tag, self.category)

    def _label_ids(self) -> set[int]:
        """The pin's current label primary keys, refreshed from the database."""
        return set(self.pin.labels.values_list("pk", flat=True))

    def _tombstoned(self) -> set[str]:
        """The label values tombstoned on the fixture pin."""
        return set(PinAutoRemoval.objects.filter(pin=self.pin, kind=AutoRemovalKind.LABEL).values_list("value", flat=True))

    def test_the_submitted_set_becomes_the_pin_s_whole_organize_label_set(self) -> None:
        response = self._patch({"label_uuids": [str(self.status.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {self.status.pk})

    def test_every_label_dropped_by_the_replacement_is_tombstoned(self) -> None:
        """Without the tombstone the removal does not stick: keyword and AI
        auto-tagging re-derive labels from the pin's own text and would put the
        label straight back on their next run."""
        response = self._patch({"label_uuids": [str(self.category.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._tombstoned(), {str(self.tag.pk)})

    def test_an_empty_list_removes_every_organize_label_and_tombstones_each(self) -> None:
        response = self._patch({"label_uuids": []})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), set())
        self.assertEqual(self._tombstoned(), {str(self.tag.pk), str(self.category.pk)})

    def test_a_label_kept_by_the_replacement_is_not_tombstoned(self) -> None:
        response = self._patch({"label_uuids": [str(self.tag.uuid), str(self.category.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {self.tag.pk, self.category.pk})
        self.assertEqual(self._tombstoned(), set())

    def test_person_labels_survive_a_replacement_that_never_mentions_them(self) -> None:
        """Person and media labels are attached by entirely different surfaces
        (photo tagging, media galleries). Stripping them here would delete data
        from a UI the user was not even looking at."""
        person = self._label("Sam", KIND_USER)
        self.pin.labels.add(person)

        response = self._patch({"label_uuids": []})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {person.pk})
        self.assertNotIn(str(person.pk), self._tombstoned())

    def test_a_global_label_is_accepted(self) -> None:
        """Labels with no owner are usable by everyone - they are not "someone else's"."""
        shared = Label.objects.create(name="urbex", kind=KIND_TAG, profile=None)

        response = self._patch({"label_uuids": [str(shared.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {shared.pk})

    def test_an_unknown_label_uuid_is_a_400_not_a_silent_skip(self) -> None:
        """A partial resolution answered 200 would hand the client a label set
        smaller than the one it submitted, with nothing saying so."""
        response = self._patch({"label_uuids": [str(self.status.uuid), str(uuid4())]})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("label_uuids", response.json()["error"])
        self.assertEqual(self._label_ids(), {self.tag.pk, self.category.pk})
        self.assertEqual(self._tombstoned(), set())

    def test_another_users_private_label_is_a_400(self) -> None:
        theirs = self._label("their tag", KIND_TAG, profile=self.other_profile)

        response = self._patch({"label_uuids": [str(theirs.uuid)]})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self._label_ids(), {self.tag.pk, self.category.pk})

    def test_a_person_label_uuid_is_a_400_rather_than_a_quiet_no_op(self) -> None:
        """Only tag/category/status labels are this payload's to set, so a
        person label here is a client bug worth reporting, not one to swallow."""
        person = self._label("Sam", KIND_USER)

        response = self._patch({"label_uuids": [str(person.uuid)]})

        self.assertEqual(response.status_code, 400, response.content)

    def test_omitting_label_uuids_entirely_leaves_the_labels_alone(self) -> None:
        response = self._patch({"description": "unrelated edit"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {self.tag.pk, self.category.pk})
        self.assertEqual(self._tombstoned(), set())

    def test_repeating_a_uuid_in_the_submitted_list_is_harmless(self) -> None:
        response = self._patch({"label_uuids": [str(self.tag.uuid), str(self.tag.uuid)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._label_ids(), {self.tag.pk})


class PinPatchVisitedTests(_PinPatchTestCase):
    """``visited`` is a convenience over the profile's "Visited" status label."""

    def setUp(self) -> None:
        """Resolve the profile's built-in "Visited" status label."""
        super().setUp()
        self.pin.labels.clear()
        self.visited_label = Label.objects.get(profile=self.profile, kind=KIND_STATUS, name="Visited")

    def _has_visited_label(self) -> bool:
        """Whether the fixture pin currently carries the "Visited" status label."""
        return self.pin.labels.filter(pk=self.visited_label.pk).exists()

    def test_visited_true_adds_the_visited_status_label(self) -> None:
        response = self._patch({"visited": True})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self._has_visited_label())
        self.assertEqual(self._get().json()["status"], "Visited")

    def test_visited_true_twice_is_idempotent(self) -> None:
        self._patch({"visited": True})

        response = self._patch({"visited": True})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.pin.labels.filter(pk=self.visited_label.pk).count(), 1)

    def test_visited_false_removes_the_label_and_clears_last_visited(self) -> None:
        """Both halves, because the point of un-marking a pin is to get it back
        into the Memories "log your visits" queue - which a stale
        ``last_visited`` would keep it out of."""
        self._patch({"last_visited": "2024-06-01T12:00:00Z"})
        self._patch({"visited": True})

        response = self._patch({"visited": False})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self._has_visited_label())
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.last_visited)

    def test_visited_combined_with_an_explicit_last_visited_is_a_400(self) -> None:
        """The two make opposite claims about the same fact - ``visited: false``
        clears ``last_visited`` outright - so any precedence rule would silently
        discard one of the two things the client actually asked for."""
        response = self._patch({"visited": True, "last_visited": "2024-06-01T12:00:00Z"})

        self.assertEqual(response.status_code, 400, response.content)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.last_visited)
        self.assertFalse(self._has_visited_label())

    def test_visited_false_with_an_explicit_last_visited_is_also_a_400(self) -> None:
        response = self._patch({"visited": False, "last_visited": "2024-06-01T12:00:00Z"})

        self.assertEqual(response.status_code, 400, response.content)

    def test_visited_true_wins_over_a_label_replacement_that_omits_it(self) -> None:
        """Applied after the replacement on purpose: a client sending its whole
        label set plus ``visited: true`` means both, and undoing one of them is
        not a defensible reading."""
        tag = self._label("rooftop", KIND_TAG)

        response = self._patch({"label_uuids": [str(tag.uuid)], "visited": True})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(self.pin.labels.values_list("pk", flat=True)), {tag.pk, self.visited_label.pk})

    def test_omitting_visited_leaves_the_marking_alone(self) -> None:
        self._patch({"visited": True})

        response = self._patch({"description": "unrelated edit"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self._has_visited_label())


class PinPatchWikiStatVoteTests(_PinPatchTestCase):
    """Writing priority/danger/vulnerability publishes a *community* wiki vote.

    This is the surprising one. A client's user believes they are adjusting a
    private star rating on their own pin; when that pin is attached to a
    community wiki and the matching ``sync_*_to_wiki`` setting is on, the value
    becomes their ``WikiStatVote`` and feeds the composite score every other
    person with access to that wiki sees. If that ever stops being true - or
    starts happening for fields it should not - a client's privacy copy is
    wrong, so it is asserted rather than left to the signal's own tests.
    """

    def setUp(self) -> None:
        """Attach the fixture pin to a community wiki at its own Location."""
        super().setUp()
        self.wiki = baker.make("dashboard.Wiki", location=self.pin.location, name="Old Mill")
        Pin.objects.filter(pk=self.pin.pk).update(wiki=self.wiki)
        self.pin.refresh_from_db()

    def _votes(self) -> dict[str, int]:
        """The owner's votes on the fixture wiki, keyed by stat field."""
        return dict(WikiStatVote.objects.filter(wiki=self.wiki, profile=self.profile).values_list("field", "value"))

    def _patch_committed(self, payload: dict):
        """PATCH with ``transaction.on_commit`` hooks actually run.

        The vote is published from an ``on_commit`` callback, which a Django
        ``TestCase``'s wrapping transaction never reaches - without this the
        assertion would pass vacuously against an empty table.

        Args:
            payload: The JSON body to send.

        Returns:
            The Django test-client response.
        """
        with self.captureOnCommitCallbacks(execute=True):
            return self._patch(payload)

    def test_writing_priority_publishes_the_owners_community_vote(self) -> None:
        response = self._patch_committed({"priority": 4})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._votes(), {"priority": 4})

    def test_danger_and_vulnerability_publish_their_own_votes(self) -> None:
        response = self._patch_committed({"danger": 5, "vulnerability": 2})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._votes(), {"danger": 5, "vulnerability": 2})

    def test_setting_the_value_back_to_zero_withdraws_the_vote(self) -> None:
        """0 means "unset" on the pin, and a 0-valued vote would skew the wiki's
        composite average rather than leaving it."""
        self._patch_committed({"priority": 4})

        response = self._patch_committed({"priority": 0})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._votes(), {})

    def test_an_unrelated_field_edit_publishes_nothing(self) -> None:
        """The narrow ``update_fields`` is what makes this true: a save naming
        every column would republish the owner's votes on every edit, including
        ones that never touched a stat."""
        response = self._patch_committed({"description": "Boiler house", "color": "#0088CC"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._votes(), {})

    def test_no_vote_is_published_when_the_owner_turned_that_sync_off(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(sync_priority_to_wiki=False)

        response = self._patch_committed({"priority": 4, "danger": 4})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._votes(), {"danger": 4})

    def test_a_pin_with_no_wiki_publishes_nothing(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(wiki=None)

        response = self._patch_committed({"priority": 4})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(WikiStatVote.objects.exists())


class PinPatchWikiLossConfirmationTests(_PinPatchTestCase):
    """A move that costs the owner wiki access needs an explicit acknowledgement.

    Wiki visibility follows where a person's pins are, so dragging a pin off a
    site quietly revokes their access to that site's community wiki. The 409
    handshake is what turns that into a decision instead of a surprise.
    """

    def setUp(self) -> None:
        """Give the fixture pin's Location a community wiki."""
        super().setUp()
        self.wiki = baker.make("dashboard.Wiki", location=self.pin.location, name="Old Mill")

    def test_a_move_that_costs_wiki_access_is_a_409_naming_the_wikis(self) -> None:
        response = self._patch({"latitude": 10.0, "longitude": 10.0})

        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertTrue(body["requires_wiki_loss_confirmation"])
        self.assertEqual([wiki["name"] for wiki in body["wikis"]], ["Old Mill"])

    def test_the_refused_move_leaves_the_pin_exactly_where_it_was(self) -> None:
        original_location_id = self.pin.location_id

        self._patch({"latitude": 10.0, "longitude": 10.0, "description": "moved"})

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, original_location_id)
        self.assertNotEqual(self.pin.description, "moved")

    def test_confirm_wiki_loss_lets_the_move_through(self) -> None:
        original_location_id = self.pin.location_id

        response = self._patch({"latitude": 10.0, "longitude": 10.0, "confirm_wiki_loss": True})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.location_id, original_location_id)

    def test_a_move_that_costs_nothing_needs_no_confirmation(self) -> None:
        """An empty result is the normal case, so the prompt must not fire for it."""
        self.wiki.delete()

        response = self._patch({"latitude": 10.0, "longitude": 10.0})

        self.assertEqual(response.status_code, 200, response.content)

    def test_the_handshake_is_asked_only_once_the_rest_of_the_request_is_valid(self) -> None:
        """Confirming a move and then being handed a 400 for an unrelated bad
        field would be a pointless prompt."""
        response = self._patch({"latitude": 10.0, "longitude": 10.0, "priority": 99})

        self.assertEqual(response.status_code, 400, response.content)


class PinPatchNotWritableHereTests(_PinPatchTestCase):
    """Fields a client may reasonably expect here, and must not find."""

    def test_rating_is_not_writable_through_this_endpoint(self) -> None:
        """A pin's rating is not a pin field - it is the caller's ``Review`` of
        it, written through ``PUT``/``DELETE /pins/{slug}/review/``. Two write
        paths to one value would have to be kept in agreement forever."""
        self.assertNotIn("rating", PinUpdateSerializer().fields)

        response = self._patch({"rating": 5})

        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.rating, 0)
        self.assertEqual(self._get().json()["rating"], 0)

    def test_address_is_not_writable_through_this_endpoint(self) -> None:
        """``address``/``city``/``state``/``country`` are not stored on the pin
        at all: they are derived from the shared ``Location`` it points at, and
        several people's pins can share one Location. A pin is moved by sending
        coordinates, never by rewriting an address."""
        for field in ("address", "city", "state", "country", "official_name"):
            with self.subTest(field=field):
                self.assertNotIn(field, PinUpdateSerializer().fields)

        before = self._get().json()["address"]

        response = self._patch({"address": "1 Somewhere Else Rd"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get().json()["address"], before)

    def test_the_writable_pin_columns_are_exactly_what_the_edit_service_allows(self) -> None:
        """Guards the other direction: a field added to the serializer that the
        shared edit service does not handle would be refused at runtime with a
        400 that names it - a contract break discovered by a user, not a test."""
        serializer_columns = set(PinUpdateSerializer().fields) - {"latitude", "longitude", "parent_id", "label_uuids", "visited", "confirm_wiki_loss", "security"}
        self.assertLessEqual(serializer_columns, EDITABLE_PIN_FIELDS)


class PinPatchAuthorizationTests(_PinPatchTestCase):
    """Per-method scope enforcement, and 404 (never 403) for someone else's pin."""

    def test_pins_read_alone_cannot_patch(self) -> None:
        """GET and PATCH on the same path demand different scopes."""
        read_only = self._key_with_scopes([ApiKeyScope.PINS_READ.value])

        response = self._patch({"description": "should not land"}, raw_key=read_only)

        self.assertEqual(response.status_code, 403)
        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.description, "should not land")

    def test_another_users_pin_is_a_404_never_a_403(self) -> None:
        """A 403 would confirm the pin exists, which is the thing being hidden."""
        theirs = create_pin_for_profile(self.other_profile, name="Not yours", latitude=1.0, longitude=1.0).pin

        response = self._patch({"description": "hijacked"}, pin=theirs)

        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertNotEqual(theirs.description, "hijacked")

    def test_another_users_pin_is_byte_identical_to_an_unknown_slug(self) -> None:
        theirs = create_pin_for_profile(self.other_profile, name="Not yours", latitude=1.0, longitude=1.0).pin

        hidden = self._patch({"description": "hijacked"}, pin=theirs)
        missing = self.client.patch(f"{BASE}/{uuid4()}/", {"description": "hijacked"}, content_type="application/json", **_bearer(self.raw_key))

        self.assertEqual(hidden.status_code, missing.status_code)
        self.assertEqual(hidden.content, missing.content)

    def test_no_credentials_is_rejected(self) -> None:
        response = self.client.patch(self._url(), {"description": "anon"}, content_type="application/json")

        self.assertEqual(response.status_code, 401)


class PinUpdateEditMappingTests(SimpleTestCase):
    """``PinUpdateSerializer.pin_field_edits`` - the wire-to-column flattening.

    Unit-level because this is pure parsing and the property below would be
    unbearably slow against the database. The regression it pins down is real:
    the wire key ``security`` carries the nested indicator object, but
    ``security`` is *also* the name of one of the eight indicator columns
    (``models.abstract.security.SECURITY_FIELDS``). It therefore passes the
    ``EDITABLE_PIN_FIELDS`` membership test, and a naive flat copy carried the
    whole nested dict through to ``setattr(pin, "security", {...})`` - a
    ``varchar(20)`` - so an entirely ordinary
    ``{"security": {"locked": "everywhere"}}`` died with a database
    ``DataError`` and the caller got a 500.
    """

    def _edits(self, payload: dict) -> dict[str, Any]:
        """Validate *payload* and return the flattened edit mapping.

        Args:
            payload: A pin-update wire payload.

        Returns:
            The ``Pin`` column -> value mapping the view would apply.

        Raises:
            AssertionError: The payload did not validate.
        """
        serializer = PinUpdateSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.pin_field_edits()

    def test_the_nested_security_object_never_survives_as_a_dict(self) -> None:
        edits = self._edits({"security": {"locked": SecurityLevel.EVERYWHERE.value}})

        self.assertEqual(edits, {"locked": SecurityLevel.EVERYWHERE.value})
        for value in edits.values():
            self.assertIsInstance(value, str)

    def test_the_indicator_named_security_is_written_as_a_plain_value(self) -> None:
        """The one indicator whose name matches the wire key - the trap itself."""
        edits = self._edits({"security": {"security": SecurityLevel.SOME.value}})

        self.assertEqual(edits, {"security": SecurityLevel.SOME.value})

    def test_non_column_keys_are_dropped_from_the_mapping(self) -> None:
        """Each of these has its own handling in the view; letting one through
        would reach ``apply_pin_edits``, which refuses unknown fields outright."""
        edits = self._edits(
            {
                "latitude": 42.5,
                "longitude": -73.5,
                "parent_id": str(uuid4()),
                "label_uuids": [],
                "visited": True,
                "confirm_wiki_loss": True,
                "description": "kept",
            }
        )

        self.assertEqual(edits, {"description": "kept"})

    def test_an_empty_payload_produces_an_empty_mapping(self) -> None:
        self.assertEqual(self._edits({}), {})

    @given(
        st.sets(st.sampled_from(sorted(_SAMPLE_FIELD_VALUES))),
        st.sets(st.sampled_from(sorted(SECURITY_EDIT_FIELDS))),
    )
    @hypothesis_settings(deadline=None, max_examples=60)
    def test_any_subset_of_the_optional_fields_yields_exactly_that_subset(self, fields: set[str], security_fields: set[str]) -> None:
        """The property behind "absent means untouched".

        ``apply_pin_edits`` writes precisely the columns it is handed, so a key
        appearing here that the client never sent is a silent overwrite of a
        field the user was not looking at - and one going missing is the lost
        edit this whole widening exists to end.
        """
        payload: dict[str, Any] = {name: _SAMPLE_FIELD_VALUES[name] for name in fields}
        if security_fields:
            payload["security"] = dict.fromkeys(sorted(security_fields), SecurityLevel.EVERYWHERE.value)

        edits = self._edits(payload)

        self.assertEqual(set(edits), fields | security_fields)
        self.assertLessEqual(set(edits), EDITABLE_PIN_FIELDS)
        for name in security_fields:
            self.assertEqual(edits[name], SecurityLevel.EVERYWHERE.value)

    @given(st.sets(st.sampled_from(sorted(_SAMPLE_FIELD_VALUES)), min_size=1))
    @hypothesis_settings(deadline=None, max_examples=40)
    def test_values_are_carried_through_unchanged_apart_from_typed_parsing(self, fields: set[str]) -> None:
        """DRF may coerce a date string to ``datetime.date``; nothing else moves."""
        payload = {name: _SAMPLE_FIELD_VALUES[name] for name in fields}

        edits = self._edits(payload)

        for name in fields:
            submitted = _SAMPLE_FIELD_VALUES[name]
            with self.subTest(field=name):
                if isinstance(edits[name], datetime.date):
                    self.assertEqual(edits[name].isoformat(), submitted)
                else:
                    self.assertEqual(edits[name], submitted)
