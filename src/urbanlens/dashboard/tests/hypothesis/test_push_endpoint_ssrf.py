"""A UnifiedPush endpoint is a user-supplied URL the server later POSTs to.

`register_device` stores the address and `dispatch` POSTs notification payloads
to it, so anything a user can register becomes a server-side request primitive.
The validation was right in shape but was a *copy* of the checks in
`services.security.url_safety`, and had drifted: it missed the RFC 6598 CGNAT
range (100.64.0.0/10), which the shared helper blocks precisely because Python's
`ipaddress` does not classify it as private and cloud providers route
internal-only infrastructure through it. Registration now uses the shared
`is_blocked_address`, so the two cannot diverge again.

Addresses are resolved with `getaddrinfo`, so these patch it rather than relying
on any particular hostname resolving a particular way in CI.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.push_device import PushDevice, PushTransport
from urbanlens.dashboard.services.notifications.push import PushRegistrationError, register_device


def _resolves_to(ip: str):
    """Patch getaddrinfo so a hostname resolves to exactly *ip*."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return patch(
        "urbanlens.dashboard.services.notifications.push.socket.getaddrinfo",
        return_value=[(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))],
    )


class PushEndpointSsrfTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _register(self, address: str = "https://push.example.test/UP?token=abc") -> PushDevice:
        return register_device(self.profile, transport=PushTransport.UNIFIEDPUSH, address=address, name="phone")

    def test_cgnat_address_is_refused(self) -> None:
        """The range the inline copy of this check used to miss."""
        with _resolves_to("100.64.0.1"), self.assertRaises(PushRegistrationError):
            self._register()

        self.assertFalse(PushDevice.objects.exists())

    def test_loopback_is_refused(self) -> None:
        with _resolves_to("127.0.0.1"), self.assertRaises(PushRegistrationError):
            self._register()

    def test_private_range_is_refused(self) -> None:
        for ip in ("10.0.0.5", "192.168.1.1", "172.16.0.1"):
            with _resolves_to(ip), self.assertRaises(PushRegistrationError):
                self._register()

    def test_link_local_metadata_address_is_refused(self) -> None:
        """169.254.169.254 is the cloud instance-metadata endpoint."""
        with _resolves_to("169.254.169.254"), self.assertRaises(PushRegistrationError):
            self._register()

    def test_ipv6_loopback_is_refused(self) -> None:
        with _resolves_to("::1"), self.assertRaises(PushRegistrationError):
            self._register()

    def test_embedded_credentials_are_refused(self) -> None:
        with _resolves_to("93.184.216.34"), self.assertRaises(PushRegistrationError):
            self._register("https://user:pass@push.example.test/UP")

    def test_non_http_scheme_is_refused(self) -> None:
        with self.assertRaises(PushRegistrationError):
            self._register("file:///etc/passwd")

    def test_a_public_endpoint_still_registers(self) -> None:
        """The guard must not break legitimate self-hosted push servers."""
        with _resolves_to("93.184.216.34"):
            device = self._register()

        self.assertEqual(device.profile_id, self.profile.pk)
        self.assertTrue(PushDevice.objects.filter(pk=device.pk).exists())
