"""Tests for native push: device registration endpoints, dispatch, and the notification hook.

The registration surface is part of the external API (a native client holding
an API key or OAuth2 token registers its UnifiedPush endpoint); dispatch is a
Celery task fed by the ``NotificationLog`` post_save signal. External HTTP
(the push server) is always mocked.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.push_device import PushDevice, PushTransport
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.push import (
    MAX_CONSECUTIVE_FAILURES,
    PushRegistrationError,
    register_device,
    send_push_to_profile,
    unregister_device,
)
from urbanlens.dashboard.tasks import dispatch_native_push


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _fake_resolution(host_ip: str):
    """Patch endpoint DNS resolution to return the given address."""
    return mock.patch("urbanlens.dashboard.services.push.socket.getaddrinfo", return_value=[(2, 1, 6, "", (host_ip, 443))])


class PushDeviceRegistrationServiceTests(TestCase):
    """register_device/unregister_device semantics, including SSRF guards."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def test_registers_a_public_https_endpoint(self) -> None:
        with _fake_resolution("8.8.8.8"):
            device = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC", name="Pixel 9")
        self.assertEqual(device.profile_id, self.profile.pk)
        self.assertEqual(device.name, "Pixel 9")
        self.assertIsNone(device.revoked_at)

    def test_reregistering_the_same_address_reactivates_instead_of_duplicating(self) -> None:
        with _fake_resolution("8.8.8.8"):
            first = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC")
            PushDevice.objects.filter(pk=first.pk).update(revoked_at=first.created, failure_count=5)
            second = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC", name="Renamed")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PushDevice.objects.count(), 1)
        second.refresh_from_db()
        self.assertIsNone(second.revoked_at)
        self.assertEqual(second.failure_count, 0)
        self.assertEqual(second.name, "Renamed")

    def test_non_http_scheme_is_rejected(self) -> None:
        with self.assertRaises(PushRegistrationError):
            register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="ftp://ntfy.example.com/up")

    def test_credentials_in_url_are_rejected(self) -> None:
        with self.assertRaises(PushRegistrationError):
            register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://user:pass@ntfy.example.com/up")

    def test_endpoint_resolving_to_private_address_is_rejected(self) -> None:
        """The server must never be tricked into POSTing at its own internal network."""
        for private_ip in ("127.0.0.1", "10.0.0.5", "192.168.1.20", "169.254.1.1"):
            with _fake_resolution(private_ip), self.assertRaises(PushRegistrationError):
                register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://sneaky.example.com/up")

    def test_fcm_token_skips_url_validation(self) -> None:
        device = register_device(self.profile, transport=PushTransport.FCM, address="fcm-token-abc123")
        self.assertEqual(device.transport, PushTransport.FCM)

    def test_unregister_revokes_only_own_devices(self) -> None:
        with _fake_resolution("8.8.8.8"):
            device = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC")
        other_profile = Profile.objects.get(user=baker.make(User))
        self.assertFalse(unregister_device(other_profile, device.uuid))
        self.assertTrue(unregister_device(self.profile, device.uuid))
        device.refresh_from_db()
        self.assertIsNotNone(device.revoked_at)


class PushDispatchTests(TestCase):
    """send_push_to_profile delivery bookkeeping."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        with _fake_resolution("8.8.8.8"):
            self.device = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC")

    def _respond(self, status_code: int) -> mock.Mock:
        return mock.Mock(status_code=status_code)

    def test_successful_delivery_posts_payload_and_resets_failures(self) -> None:
        PushDevice.objects.filter(pk=self.device.pk).update(failure_count=3)
        with mock.patch("urbanlens.dashboard.services.push.requests.post", return_value=self._respond(200)) as post:
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})
        self.assertEqual(delivered, 1)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://ntfy.example.com/upABC")
        self.assertEqual(post.call_args.kwargs["json"], {"title": "Hi"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.failure_count, 0)
        self.assertIsNotNone(self.device.last_success_at)

    def test_failed_delivery_increments_failure_count(self) -> None:
        with mock.patch("urbanlens.dashboard.services.push.requests.post", return_value=self._respond(500)):
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})
        self.assertEqual(delivered, 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.failure_count, 1)
        self.assertIsNone(self.device.revoked_at)

    def test_device_is_auto_revoked_after_consecutive_failures(self) -> None:
        PushDevice.objects.filter(pk=self.device.pk).update(failure_count=MAX_CONSECUTIVE_FAILURES - 1)
        with mock.patch("urbanlens.dashboard.services.push.requests.post", return_value=self._respond(500)):
            send_push_to_profile(self.profile.pk, {"title": "Hi"})
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.revoked_at)

    def test_revoked_devices_are_never_dispatched_to(self) -> None:
        unregister_device(self.profile, self.device.uuid)
        with mock.patch("urbanlens.dashboard.services.push.requests.post") as post:
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})
        self.assertEqual(delivered, 0)
        post.assert_not_called()

    def test_fcm_devices_are_skipped_for_now(self) -> None:
        register_device(self.profile, transport=PushTransport.FCM, address="fcm-token")
        with mock.patch("urbanlens.dashboard.services.push.requests.post", return_value=self._respond(200)) as post:
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})
        self.assertEqual(delivered, 1)  # the UnifiedPush device only
        post.assert_called_once()

    def test_dispatch_task_serializes_the_notification(self) -> None:
        notification = NotificationLog.objects.create(profile=self.profile, title="New comment", message="Someone replied")
        with mock.patch("urbanlens.dashboard.services.push.requests.post", return_value=self._respond(200)) as post:
            delivered = dispatch_native_push(notification.pk)
        self.assertEqual(delivered, 1)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["title"], "New comment")
        self.assertEqual(payload["id"], notification.pk)


class NotificationSignalTests(TestCase):
    """Creating a NotificationLog enqueues native push delivery after commit."""

    def test_insert_enqueues_the_dispatch_task(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        user = baker.make(User)
        profile = Profile.objects.get(user=user)
        with mock.patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            notification = NotificationLog.objects.create(profile=profile, title="Hello", message="World")
        enqueued_ids = [call.args[1] for call in enqueue.call_args_list if getattr(call.args[0], "name", "").endswith("dispatch_native_push")]
        self.assertIn(notification.pk, enqueued_ids)


class PushDeviceEndpointTests(TestCase):
    """The external API's push-device registration surface."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:push_devices")
        _api_key, self.raw_key = generate_api_key(self.user, "Mobile app")

    def _post(self, payload: dict):
        return self.client.post(self.url, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_register_returns_the_device_uuid_and_never_the_address(self) -> None:
        with _fake_resolution("8.8.8.8"):
            response = self._post({"address": "https://ntfy.example.com/upABC", "name": "Pixel 9"})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertNotIn("address", body)
        device = PushDevice.objects.get(uuid=body["uuid"])
        self.assertEqual(device.profile_id, self.profile.pk)

    def test_invalid_endpoint_is_a_clean_400(self) -> None:
        response = self._post({"address": "ftp://nope.example.com/up"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_requires_push_manage_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._post({"address": "https://ntfy.example.com/upABC"})
        self.assertEqual(response.status_code, 403)

    def test_no_credentials_is_rejected(self) -> None:
        response = self.client.post(self.url, data={"address": "https://ntfy.example.com/up"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_delete_unregisters_own_device(self) -> None:
        with _fake_resolution("8.8.8.8"):
            device = register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upABC")
        url = reverse("external_api:push_devices.detail", kwargs={"device_uuid": device.uuid})
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 204)
        device.refresh_from_db()
        self.assertIsNotNone(device.revoked_at)

    def test_delete_of_another_users_device_is_not_found(self) -> None:
        other_profile = Profile.objects.get(user=baker.make(User))
        with _fake_resolution("8.8.8.8"):
            device = register_device(other_profile, transport=PushTransport.UNIFIEDPUSH, address="https://ntfy.example.com/upOTHER")
        url = reverse("external_api:push_devices.detail", kwargs={"device_uuid": device.uuid})
        response = self.client.delete(url, **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        device.refresh_from_db()
        self.assertIsNone(device.revoked_at)
