"""External API: notification inbox, acknowledgement, and delivery preferences.

The acknowledgement endpoint is the security-sensitive one. It answers 204
whether or not a row matched, so a caller cannot use it to discover whether a
given uuid belongs to *somebody* - the same reasoning behind
``services.push.unregister_device`` returning a bare bool.
"""

from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.notification_center import preference_field_names


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for the test client.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class NotificationEnumValueTests(TestCase):
    """These enums are lowercase snake_case - unlike FriendshipStatus."""

    def test_status_values(self) -> None:
        self.assertEqual({value for value, _label in Status.choices}, {"unread", "read", "dismissed"})

    def test_importance_values(self) -> None:
        self.assertEqual({value for value, _label in Importance.choices}, {"lowest", "low", "medium", "high", "highest"})

    def test_delivery_preference_values(self) -> None:
        self.assertEqual({value for value, _label in DeliveryPreference.choices}, {"none", "site", "email", "both"})

    def test_notification_types_are_lowercase(self) -> None:
        for value, _label in NotificationType.choices:
            with self.subTest(value=value):
                self.assertEqual(value, value.lower())


class NotificationPreferenceCoverageTests(TestCase):
    """The preference model covers only a subset of NotificationType."""

    def test_preference_stems_are_introspected_from_the_model(self) -> None:
        """Derived, not hardcoded - a thirteenth preference flows through free."""
        stems = preference_field_names()
        model_fields = {field.name for field in NotificationPreference._meta.get_fields()}
        for stem in stems:
            with self.subTest(stem=stem):
                self.assertIn(stem, model_fields)
                self.assertIn(f"{stem}_whatsapp", model_fields)
                self.assertIn(f"{stem}_sms", model_fields)

    def test_preferences_cover_only_a_subset_of_notification_types(self) -> None:
        """Documents the real gap rather than inventing defaults for the rest.

        Most ``NotificationType`` members have no preference column at all, so
        they have no per-type delivery control. The API exposes exactly the
        stems that exist; it must not fabricate entries for the others.
        """
        stems = set(preference_field_names())
        all_types = {value for value, _label in NotificationType.choices}
        self.assertEqual(len(stems), 12)
        self.assertLess(len(stems), len(all_types))

    def test_one_preference_stem_does_not_match_its_notification_type(self) -> None:
        """Pins a real naming mismatch between the two enums.

        ``NotificationType.SAFETY_CHECKIN_PARTNER_INVITE`` has the value
        ``safety_ci_partner_invite``, but its preference columns are named
        ``safety_checkin_partner_invite*``. Anything deriving a field name from
        a notification's type therefore misses it -
        ``services.notification_text_alerts._wants_text_alerts`` does exactly
        that and falls back to ``False``, so WhatsApp/SMS alerts for that type
        never fire even when the user enabled them. Recorded in
        ``docs/PROBLEMS.md``.

        This test asserts the mismatch so that fixing it (renaming either side,
        with a migration) fails here and prompts a deliberate update rather
        than silently changing the external API's field names.
        """
        stems = set(preference_field_names())
        all_types = {value for value, _label in NotificationType.choices}
        self.assertEqual(stems - all_types, {"safety_checkin_partner_invite"})
        self.assertIn("safety_ci_partner_invite", all_types)


class NotificationInboxTests(TestCase):
    """Reading, counting and acknowledging the caller's own notifications."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other = Profile.objects.get(user=baker.make(User))

        api_key, self.raw_key = generate_api_key(self.user, "Test")
        api_key.scopes = [ApiKeyScope.NOTIFICATIONS_READ.value, ApiKeyScope.NOTIFICATIONS_WRITE.value]
        api_key.save(update_fields=["scopes"])

    def _notify(self, profile: Profile, *, status: str = Status.UNREAD) -> NotificationLog:
        """Create one notification for ``profile``.

        Args:
            profile: The recipient.
            status: The notification's status.

        Returns:
            The created row.
        """
        return NotificationLog.objects.create(
            profile=profile,
            status=status,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.INFO,
            title="Hello",
            message="Body",
        )

    def test_list_returns_only_the_callers_notifications(self) -> None:
        mine = self._notify(self.profile)
        self._notify(self.other)

        response = self.client.get(reverse("external_api:notifications"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([row["uuid"] for row in results], [str(mine.uuid)])

    def test_unread_count_is_correct(self) -> None:
        self._notify(self.profile)
        self._notify(self.profile)
        self._notify(self.profile, status=Status.READ)
        self._notify(self.other)

        response = self.client.get(reverse("external_api:notifications.unread_count"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"unread_count": 2})

    def test_unread_only_filter(self) -> None:
        unread = self._notify(self.profile)
        self._notify(self.profile, status=Status.READ)

        response = self.client.get(reverse("external_api:notifications"), {"unread_only": "true"}, **_bearer(self.raw_key))

        results = response.json()["results"]
        self.assertEqual([row["uuid"] for row in results], [str(unread.uuid)])

    def test_marking_own_notification_read(self) -> None:
        notification = self._notify(self.profile)
        url = reverse("external_api:notifications.detail", kwargs={"notification_uuid": notification.uuid})

        response = self.client.post(url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 204)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Status.READ)

    def test_foreign_uuid_and_nonexistent_uuid_are_indistinguishable(self) -> None:
        """Neither may reveal whether the uuid belongs to anybody."""
        foreign = self._notify(self.other)

        foreign_response = self.client.post(
            reverse("external_api:notifications.detail", kwargs={"notification_uuid": foreign.uuid}),
            **_bearer(self.raw_key),
        )
        missing_response = self.client.post(
            reverse("external_api:notifications.detail", kwargs={"notification_uuid": uuid4()}),
            **_bearer(self.raw_key),
        )

        self.assertEqual(foreign_response.status_code, 204)
        self.assertEqual(missing_response.status_code, 204)
        self.assertEqual(foreign_response.content, missing_response.content)
        self.assertEqual(sorted(foreign_response.headers), sorted(missing_response.headers))

        # And critically, the other profile's notification was NOT touched.
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, Status.UNREAD)

    def test_read_all_clears_only_the_callers_notifications(self) -> None:
        self._notify(self.profile)
        self._notify(self.profile)
        theirs = self._notify(self.other)

        response = self.client.post(reverse("external_api:notifications.read_all"), **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"unread_count": 0})
        self.assertEqual(NotificationLog.objects.for_profile(self.profile).unread().count(), 0)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, Status.UNREAD)

    def test_notifications_require_the_notifications_scope(self) -> None:
        _api_key, default_raw = generate_api_key(self.user, "Default")
        response = self.client.get(reverse("external_api:notifications"), **_bearer(default_raw))
        self.assertEqual(response.status_code, 403)

    def test_read_scope_does_not_permit_marking_read(self) -> None:
        api_key, raw_key = generate_api_key(self.user, "ReadOnly")
        api_key.scopes = [ApiKeyScope.NOTIFICATIONS_READ.value]
        api_key.save(update_fields=["scopes"])
        notification = self._notify(self.profile)

        response = self.client.post(
            reverse("external_api:notifications.detail", kwargs={"notification_uuid": notification.uuid}),
            **_bearer(raw_key),
        )

        self.assertEqual(response.status_code, 403)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Status.UNREAD)

    def test_pagination_walks_every_row_exactly_once(self) -> None:
        created = [self._notify(self.profile) for _ in range(5)]

        seen: list[str] = []
        cursor = None
        for _ in range(10):  # generous bound; the loop breaks on its own
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = self.client.get(reverse("external_api:notifications"), params, **_bearer(self.raw_key)).json()
            seen.extend(row["uuid"] for row in body["results"])
            cursor = body["next_cursor"]
            if not cursor:
                break

        self.assertEqual(sorted(seen), sorted(str(row.uuid) for row in created))

    def test_malformed_cursor_is_a_400(self) -> None:
        response = self.client.get(reverse("external_api:notifications"), {"cursor": "not-a-cursor"}, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)


class NotificationPreferenceRoundTripTests(TestCase):
    """PATCH/GET of the delivery matrix, using the model's real field names."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        api_key, self.raw_key = generate_api_key(self.user, "Test")
        api_key.scopes = [ApiKeyScope.NOTIFICATIONS_READ.value, ApiKeyScope.NOTIFICATIONS_WRITE.value]
        api_key.save(update_fields=["scopes"])
        self.url = reverse("external_api:notification_preferences")

    def test_get_exposes_every_real_stem(self) -> None:
        response = self.client.get(self.url, **_bearer(self.raw_key))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), set(preference_field_names()))

    def test_patch_round_trip(self) -> None:
        response = self.client.patch(
            self.url,
            {"friend_request": {"delivery": DeliveryPreference.NONE}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["friend_request"]["delivery"], "none")

        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertEqual(prefs.friend_request, DeliveryPreference.NONE)

    def test_patch_leaves_unnamed_stems_untouched(self) -> None:
        self.client.patch(
            self.url,
            {"friend_request": {"delivery": DeliveryPreference.NONE}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertEqual(prefs.message, DeliveryPreference.SITE)

    def test_whatsapp_cannot_be_enabled_without_a_number(self) -> None:
        """Forced off server-side, regardless of what the client submitted."""
        self.assertEqual(self.profile.whatsapp_number, "")

        response = self.client.patch(
            self.url,
            {"friend_request": {"whatsapp": True}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["friend_request"]["whatsapp"])
        prefs = NotificationPreference.objects.get(profile=self.profile)
        self.assertFalse(prefs.friend_request_whatsapp)

    def test_whatsapp_can_be_enabled_with_a_number(self) -> None:
        self.profile.whatsapp_number = "+15550001111"
        self.profile.save()

        response = self.client.patch(
            self.url,
            {"friend_request": {"whatsapp": True}},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertTrue(response.json()["friend_request"]["whatsapp"])

    def test_read_scope_cannot_patch_preferences(self) -> None:
        api_key, raw_key = generate_api_key(self.user, "ReadOnly")
        api_key.scopes = [ApiKeyScope.NOTIFICATIONS_READ.value]
        api_key.save(update_fields=["scopes"])

        response = self.client.patch(
            self.url,
            {"friend_request": {"delivery": DeliveryPreference.NONE}},
            content_type="application/json",
            **_bearer(raw_key),
        )

        self.assertEqual(response.status_code, 403)
