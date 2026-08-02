"""Tests for the external API's safety check-in surface.

Safety is the most sensitive domain on this surface: the payloads carry
emergency-contact addresses, destination plans, and the ability to invite a
partner into a live check-in. The tests here hold four lines in particular:

* the ``safety:*`` scopes are opt-in and absent from the default key grant, so a
  key issued today reaches none of this;
* another profile's check-in is *not found*, never forbidden;
* the contact-portal ``token`` never appears in any payload;
* PATCH honors the same field locks as the web autosave, reporting ignored
  fields as warnings rather than failing the request.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyCheckinPartner, SafetyCheckinStatus
from urbanlens.dashboard.models.undo import UndoAction
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.visits.safety import create_checkin, save_contact_defaults


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SafetyApiTestCase(TestCase):
    """Shared setup: an owner with a safety-scoped key and one active check-in."""

    scopes: list[str] = [ApiKeyScope.SAFETY_READ.value, ApiKeyScope.SAFETY_WRITE.value]

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        key, self.raw_key = generate_api_key(self.user, "Test")
        # scopes is editable=False, so it is set directly rather than through a
        # form. The default grant deliberately excludes safety:* - see
        # _default_api_key_scopes.
        ApiKey.objects.filter(pk=key.pk).update(scopes=self.scopes)

        self.checkin = create_checkin(
            profile=self.profile,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )
        self.list_url = reverse("external_api:safety.checkins")
        self.detail_url = reverse("external_api:safety.checkins.detail", kwargs={"checkin_slug": self.checkin.slug})

    def _resolve_existing(self) -> None:
        """Retire the setUp check-in so a fresh one can be created.

        Must move ``status`` to a terminal value, not just stamp ``resolved_at``:
        ``SafetyCheckin.objects.active()`` - the queryset enforcing one active
        check-in per scope - keys off status alone.
        """
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.CHECKED_IN, resolved_at=timezone.now())


class SafetyScopeTests(_SafetyApiTestCase):
    """Each method honors only the scope it declares."""

    def test_list_requires_safety_read(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self.list_url, **_bearer(self.raw_key)).status_code, 403)

    def test_create_requires_safety_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.SAFETY_READ.value])
        response = self.client.post(self.list_url, {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_patch_requires_safety_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.SAFETY_READ.value])
        response = self.client.patch(self.detail_url, {"title": "x"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_delete_requires_safety_write(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.SAFETY_READ.value])
        self.assertEqual(self.client.delete(self.detail_url, **_bearer(self.raw_key)).status_code, 403)

    def test_unauthenticated_request_is_rejected(self) -> None:
        self.assertIn(self.client.get(self.list_url).status_code, (401, 403))

    def test_default_api_key_grant_cannot_reach_safety(self) -> None:
        """The security line this whole surface rests on.

        Emergency-contact addresses and partner-invite ability are categorically
        more sensitive than pins. Every key issued before these endpoints existed
        must stay unable to reach them - silently widening those grants would be
        an unconsented privilege escalation, not a convenience.
        """
        _key, raw = generate_api_key(self.user, "Default grant")
        self.assertEqual(self.client.get(self.list_url, **_bearer(raw)).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url, **_bearer(raw)).status_code, 403)


class SafetyOwnerIsolationTests(_SafetyApiTestCase):
    """Another profile's check-in is invisible, not merely forbidden."""

    def setUp(self) -> None:
        super().setUp()
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)
        self.other_checkin = create_checkin(
            profile=self.other_profile,
            title="Someone else's trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=3),
            grace_period=datetime.timedelta(hours=1),
        )
        self.other_url = reverse("external_api:safety.checkins.detail", kwargs={"checkin_slug": self.other_checkin.slug})

    def test_other_profiles_checkin_is_404_not_403(self) -> None:
        """A 403 would confirm the slug names a real check-in belonging to someone."""
        response = self.client.get(self.other_url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_other_profiles_checkin_cannot_be_patched(self) -> None:
        response = self.client.patch(self.other_url, {"title": "Hijacked"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.other_checkin.refresh_from_db()
        self.assertEqual(self.other_checkin.title, "Someone else's trip")

    def test_other_profiles_checkin_cannot_be_deleted(self) -> None:
        self.assertEqual(self.client.delete(self.other_url, **_bearer(self.raw_key)).status_code, 404)
        self.assertTrue(SafetyCheckin.objects.filter(pk=self.other_checkin.pk).exists())

    def test_list_excludes_other_profiles_checkins(self) -> None:
        payload = self.client.get(self.list_url, **_bearer(self.raw_key)).json()
        self.assertEqual([row["uuid"] for row in payload["results"]], [str(self.checkin.uuid)])

    def test_unknown_identifier_is_404(self) -> None:
        """A non-uuid string must read as not-found, not raise a 500."""
        url = reverse("external_api:safety.checkins.detail", kwargs={"checkin_slug": "not-a-real-checkin"})
        self.assertEqual(self.client.get(url, **_bearer(self.raw_key)).status_code, 404)


class SafetyContactTokenExposureTests(_SafetyApiTestCase):
    """The contact-portal token must never leave the server.

    ``SafetyCheckinContact.token`` is the sole credential for the session-free
    contact portal: holding it means being able to read the check-in, post to its
    chat, and mark the owner safe. Leaking it to an API key would hand out portal
    access for every contact.
    """

    def _assert_no_token_anywhere(self, payload: object) -> None:
        """Recursively assert no token key, and no contact token value, appears."""
        tokens = {str(token) for token in SafetyCheckinContact.objects.values_list("token", flat=True)}
        self.assertTrue(tokens, "expected at least one contact to exist for this assertion to mean anything")

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotEqual(key, "token", f"contact token key leaked in payload: {node}")
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str):
                self.assertNotIn(node, tokens, f"contact token value leaked in payload: {node}")

        walk(payload)

    def test_detail_payload_has_no_contact_token(self) -> None:
        response = self.client.get(self.detail_url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["contacts"], "check-in should have a contact")
        self._assert_no_token_anywhere(response.json())

    def test_list_payload_has_no_contact_token(self) -> None:
        self._assert_no_token_anywhere(self.client.get(self.list_url, **_bearer(self.raw_key)).json())

    def test_patch_payload_has_no_contact_token(self) -> None:
        response = self.client.patch(self.detail_url, {"plan_details": "Updated"}, content_type="application/json", **_bearer(self.raw_key))
        self._assert_no_token_anywhere(response.json())

    def test_contact_defaults_payload_has_no_contact_token(self) -> None:
        url = reverse("external_api:safety.contacts")
        self._assert_no_token_anywhere(self.client.get(url, **_bearer(self.raw_key)).json())


class SafetyListTests(_SafetyApiTestCase):
    """The list endpoint's envelope, filters, and serialization contract."""

    def test_pagination_envelope_shape(self) -> None:
        payload = self.client.get(self.list_url, **_bearer(self.raw_key)).json()
        self.assertEqual(set(payload), {"count", "next", "previous", "results"})
        self.assertEqual(payload["count"], 1)

    def test_grace_period_is_emitted_as_integer_seconds(self) -> None:
        """Never a raw DurationField - a mobile client can't parse "01:00:00"."""
        row = self.client.get(self.list_url, **_bearer(self.raw_key)).json()["results"][0]
        self.assertEqual(row["grace_period_seconds"], 3600)
        self.assertIsInstance(row["grace_period_seconds"], int)

    def test_counts_come_from_annotations(self) -> None:
        row = self.client.get(self.list_url, **_bearer(self.raw_key)).json()["results"][0]
        self.assertEqual(row["contact_count"], 1)
        self.assertEqual(row["partner_count"], 0)

    def test_status_filter_active_and_resolved(self) -> None:
        self._resolve_existing()

        active = self.client.get(self.list_url, {"status": "active"}, **_bearer(self.raw_key)).json()
        resolved = self.client.get(self.list_url, {"status": "resolved"}, **_bearer(self.raw_key)).json()

        self.assertEqual(active["count"], 0)
        self.assertEqual(resolved["count"], 1)

    def test_live_location_fields_are_absent(self) -> None:
        """Live location is deliberately out of scope for this pass."""
        payload = self.client.get(self.detail_url, **_bearer(self.raw_key)).json()
        self.assertFalse([key for key in payload if key.startswith("live_")], payload.keys())


class SafetyCreateTests(_SafetyApiTestCase):
    """POST semantics: contact defaults, conflicts, and validation."""

    def _future(self, hours: int = 8) -> str:
        return (timezone.now() + datetime.timedelta(hours=hours)).isoformat()

    def test_creating_while_one_is_active_is_409(self) -> None:
        """A state conflict, not a malformed request - the client must tell them apart."""
        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future(), "title": "Second"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())

    def test_past_checkin_by_is_rejected(self) -> None:
        self._resolve_existing()
        response = self.client.post(
            self.list_url,
            {"checkin_by": (timezone.now() - datetime.timedelta(hours=1)).isoformat()},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_grace_period_floor_is_enforced(self) -> None:
        self._resolve_existing()
        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future(), "grace_period_seconds": 60},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_trip_is_404(self) -> None:
        self._resolve_existing()
        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future(), "trip": "no-such-trip"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 404)

    def test_non_connection_username_contact_is_rejected_with_400(self) -> None:
        """Creation is all-or-nothing: a refused contact fails the whole request."""

        self._resolve_existing()
        baker.make(User, username="astranger")
        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future(), "contacts": [{"username": "astranger"}]},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("connections", response.json()["error"])

    def test_explicit_empty_contacts_is_not_the_same_as_omitted(self) -> None:
        """``[]`` means "no contacts"; omitted means "use my saved defaults"."""

        self._resolve_existing()
        save_contact_defaults(self.profile, [(None, "default@example.com", "Default")])

        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future(), "contacts": []},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["contacts"], [])

    def test_omitted_contacts_uses_saved_defaults(self) -> None:
        self._resolve_existing()
        save_contact_defaults(self.profile, [(None, "default@example.com", "Default")])

        response = self.client.post(
            self.list_url,
            {"checkin_by": self._future()},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual([c["email"] for c in response.json()["contacts"]], ["default@example.com"])


class SafetyPatchTests(_SafetyApiTestCase):
    """PATCH honors field locks, reports warnings, and stays strictly partial."""

    def _patch(self, body: dict):
        return self.client.patch(self.detail_url, body, content_type="application/json", **_bearer(self.raw_key))

    def test_plan_update_succeeds_with_no_warnings(self) -> None:
        response = self._patch({"plan_details": "Changed route"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["warnings"], [])
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.plan_details, "Changed route")

    def test_omitted_fields_are_untouched(self) -> None:
        """The no-default rule: a field the client never sent must not move."""
        self._patch({"plan_details": "Only this"})
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "Quarry trip")
        self.assertEqual(self.checkin.contact_message, "Please call me")

    def test_locked_title_is_ignored_and_reported_as_warning_not_error(self) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(escalated_at=timezone.now())

        response = self._patch({"title": "Renamed", "plan_details": "But the plan still lands"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("Title is locked" in warning for warning in response.json()["warnings"]))
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.title, "Quarry trip")
        # The unrelated field in the same request still applied - a locked field
        # must not become collateral damage.
        self.assertEqual(self.checkin.plan_details, "But the plan still lands")

    def test_patch_on_archived_checkin_is_409(self) -> None:
        """Defect #1 at the HTTP boundary: no writing plaintext back onto a scrubbed row."""

        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(archive_scheduled_at=timezone.now())

        response = self._patch({"plan_details": "Should not be written"})

        self.assertEqual(response.status_code, 409)
        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.plan_details, "North rim, back by dark")

    def test_half_a_destination_is_rejected(self) -> None:
        self.assertEqual(self._patch({"destination_latitude": 44.0}).status_code, 400)

    def test_destination_moves_together(self) -> None:
        response = self._patch({"destination_latitude": 44.0, "destination_longitude": -73.0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["destination_latitude"], 44.0)


class SafetyLifecycleTests(_SafetyApiTestCase):
    """Mark-safe, cancel, and delete."""

    def test_mark_safe_resolves_the_checkin(self) -> None:
        url = reverse("external_api:safety.checkins.check_in", kwargs={"checkin_slug": self.checkin.slug})
        response = self.client.post(url, {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertTrue(self.checkin.is_resolved)

    def test_mark_safe_twice_is_409(self) -> None:
        url = reverse("external_api:safety.checkins.check_in", kwargs={"checkin_slug": self.checkin.slug})
        self.client.post(url, {}, content_type="application/json", **_bearer(self.raw_key))
        response = self.client.post(url, {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 409)

    def test_cancel_resolves_the_checkin(self) -> None:
        url = reverse("external_api:safety.checkins.cancel", kwargs={"checkin_slug": self.checkin.slug})
        response = self.client.post(url, {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.checkin.refresh_from_db()
        self.assertTrue(self.checkin.is_resolved)

    def test_delete_removes_the_checkin_and_stages_undo(self) -> None:
        response = self.client.delete(self.detail_url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(SafetyCheckin.objects.filter(pk=self.checkin.pk).exists())
        # Undo stashing is not optional - deletion is otherwise unrecoverable.
        self.assertTrue(UndoAction.objects.filter(profile=self.profile).exists())


class SafetyPartnerTests(_SafetyApiTestCase):
    """Partner invite error paths and removal."""

    def setUp(self) -> None:
        super().setUp()
        self.partners_url = reverse("external_api:safety.checkins.partners", kwargs={"checkin_slug": self.checkin.slug})

    def _invite(self, username: str):
        return self.client.post(self.partners_url, {"username": username}, content_type="application/json", **_bearer(self.raw_key))

    def test_unknown_username_is_400_with_the_services_own_message(self) -> None:
        response = self._invite("nobody-here")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No user found", response.json()["error"])

    def test_self_invite_is_400(self) -> None:
        response = self._invite(self.user.username)
        self.assertEqual(response.status_code, 400)
        self.assertIn("yourself", response.json()["error"])

    def test_duplicate_invite_is_400(self) -> None:
        invitee = baker.make(User, username="apartner")
        Profile.objects.get_or_create(user=invitee)

        self.assertEqual(self._invite("apartner").status_code, 200)
        response = self._invite("apartner")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already been invited", response.json()["error"])

    def test_successful_invite_returns_detail_with_partners(self) -> None:
        invitee = baker.make(User, username="apartner")
        Profile.objects.get_or_create(user=invitee)

        payload = self._invite("apartner").json()

        self.assertEqual([p["username"] for p in payload["partners"]], ["apartner"])
        self.assertEqual(payload["partner_count"], 1)

    def test_partner_removal(self) -> None:
        invitee = baker.make(User, username="apartner")
        Profile.objects.get_or_create(user=invitee)
        self._invite("apartner")
        partner = SafetyCheckinPartner.objects.get(checkin=self.checkin)

        url = reverse("external_api:safety.checkins.partners.detail", kwargs={"checkin_slug": self.checkin.slug, "partner_id": partner.pk})
        response = self.client.delete(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists())

    def test_removing_another_checkins_partner_is_404(self) -> None:
        url = reverse("external_api:safety.checkins.partners.detail", kwargs={"checkin_slug": self.checkin.slug, "partner_id": 999999})
        self.assertEqual(self.client.delete(url, **_bearer(self.raw_key)).status_code, 404)


class SafetyPreferencesApiTests(_SafetyApiTestCase):
    """The safety-defaults endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:safety.settings")

    def test_get_returns_seconds_not_a_duration(self) -> None:
        payload = self.client.get(self.url, **_bearer(self.raw_key)).json()
        self.assertIsInstance(payload["default_grace_period_seconds"], int)

    def test_patch_is_partial(self) -> None:
        self.client.patch(self.url, {"default_message": "Call me"}, content_type="application/json", **_bearer(self.raw_key))
        payload = self.client.patch(self.url, {"auto_delete_after_days": 30}, content_type="application/json", **_bearer(self.raw_key)).json()

        self.assertEqual(payload["default_message"], "Call me")
        self.assertEqual(payload["auto_delete_after_days"], 30)

    def test_grace_period_floor_is_enforced(self) -> None:
        response = self.client.patch(self.url, {"default_grace_period_seconds": 10}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)


class SafetyCheckinMapsTests(_SafetyApiTestCase):
    """The check-in maps endpoint answers with the standard paginated envelope.

    Regression coverage for the bare top-level array this endpoint used to answer
    with - it could never gain a field later without breaking clients, so it was
    normalized onto ``{count,next,previous,results}`` (see
    ``docs/notes/mobile_app_notes.md`` Part 7).
    """

    def setUp(self) -> None:
        super().setUp()
        self.maps_url = reverse("external_api:safety.checkins.maps", kwargs={"checkin_slug": self.checkin.slug})

    def test_empty_checkin_has_standard_keys_and_no_results(self) -> None:
        response = self.client.get(self.maps_url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["results"], [])

    def test_primary_and_reference_maps_are_both_listed(self) -> None:
        from urbanlens.dashboard.models.markup.model import MarkupMap

        primary = MarkupMap.objects.create(profile=self.profile, title="Route")
        reference = MarkupMap.objects.create(profile=self.profile, title="Reference")
        self.checkin.markup_map = primary
        self.checkin.save(update_fields=["markup_map"])
        self.checkin.markup_maps.add(reference)

        body = self.client.get(self.maps_url, **_bearer(self.raw_key)).json()

        self.assertEqual(body["count"], 2)
        by_uuid = {row["uuid"]: row for row in body["results"]}
        self.assertTrue(by_uuid[str(primary.uuid)]["is_primary"])
        self.assertFalse(by_uuid[str(reference.uuid)]["is_primary"])

    def test_pages_via_the_standard_page_size_param(self) -> None:
        from urbanlens.dashboard.models.markup.model import MarkupMap

        for i in range(2):
            self.checkin.markup_maps.add(MarkupMap.objects.create(profile=self.profile, title=f"Reference {i}"))

        first = self.client.get(self.maps_url, {"page_size": 1}, **_bearer(self.raw_key)).json()
        self.assertEqual(first["count"], 2)
        self.assertEqual(len(first["results"]), 1)
        self.assertIsNotNone(first["next"])

    def test_attach_response_uses_the_same_envelope(self) -> None:
        from urbanlens.dashboard.models.markup.model import MarkupMap

        own_map = MarkupMap.objects.create(profile=self.profile, title="Mine")

        response = self.client.post(self.maps_url, {"map_uuid": str(own_map.uuid)}, content_type="application/json", **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["uuid"], str(own_map.uuid))
