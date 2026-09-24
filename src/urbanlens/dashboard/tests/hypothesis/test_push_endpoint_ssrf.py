"""A UnifiedPush endpoint is a user-supplied URL the server later POSTs to."""

from __future__ import annotations

import socket
from unittest.mock import Mock, patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.push_device import PushDevice, PushTransport
from urbanlens.dashboard.services.notifications.push import (
    EndpointCredentialsError,
    EndpointUnreachableError,
    InvalidEndpointUrlError,
    register_device,
    send_push_to_profile,
)
from urbanlens.dashboard.services.security.url_safety import _PINS


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
        with _resolves_to("100.64.0.1"), self.assertRaises(EndpointUnreachableError):
            self._register()

        self.assertFalse(PushDevice.objects.exists())

    def test_loopback_is_refused(self) -> None:
        with _resolves_to("127.0.0.1"), self.assertRaises(EndpointUnreachableError):
            self._register()

    def test_private_range_is_refused(self) -> None:
        for ip in ("10.0.0.5", "192.168.1.1", "172.16.0.1"):
            with _resolves_to(ip), self.assertRaises(EndpointUnreachableError):
                self._register()

    def test_link_local_metadata_address_is_refused(self) -> None:
        """169.254.169.254 is the cloud instance-metadata endpoint."""
        with _resolves_to("169.254.169.254"), self.assertRaises(EndpointUnreachableError):
            self._register()

    def test_ipv6_loopback_is_refused(self) -> None:
        with _resolves_to("::1"), self.assertRaises(EndpointUnreachableError):
            self._register()

    def test_embedded_credentials_are_refused(self) -> None:
        with _resolves_to("93.184.216.34"), self.assertRaises(EndpointCredentialsError):
            self._register("https://user:pass@push.example.test/UP")

    def test_non_http_scheme_is_refused(self) -> None:
        with self.assertRaises(InvalidEndpointUrlError):
            self._register("file:///etc/passwd")

    def test_a_public_endpoint_still_registers(self) -> None:
        """The guard must not break legitimate self-hosted push servers."""
        with _resolves_to("93.184.216.34"):
            device = self._register()

        self.assertEqual(device.profile_id, self.profile.pk)
        self.assertTrue(PushDevice.objects.filter(pk=device.pk).exists())


class PushDispatchSsrfTests(TestCase):
    """The address check has to hold when the POST is sent, not only when the endpoint was registered."""

    POST = "urbanlens.dashboard.services.notifications.push.requests.post"

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        with _resolves_to("93.184.216.34"):
            self.device = register_device(
                self.profile, transport=PushTransport.UNIFIEDPUSH, address="https://push.example.test/UP?token=abc"
            )

    def tearDown(self) -> None:
        _PINS.map = None
        super().tearDown()

    def test_an_endpoint_that_now_resolves_internally_is_not_posted_to(self) -> None:
        """DNS rebinding: public at registration, internal at send time."""
        with _resolves_to("10.0.0.5"), patch(self.POST) as post:
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})

        post.assert_not_called()
        self.assertEqual(delivered, 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.failure_count, 1, "a refused send is a failed delivery")

    def test_the_send_does_not_follow_redirects_itself(self) -> None:
        """A 307 keeps the POST and its body, so following it is a blind SSRF to wherever it points."""
        ok = Mock(status_code=200, is_redirect=False)
        with _resolves_to("93.184.216.34"), patch(self.POST, return_value=ok) as post:
            send_push_to_profile(self.profile.pk, {"title": "Hi"})

        self.assertIs(post.call_args.kwargs.get("allow_redirects"), False)

    def test_a_redirect_is_a_failed_delivery_and_is_not_followed(self) -> None:
        redirect = Mock(status_code=307, is_redirect=True, headers={"Location": "https://elsewhere.example.test/"})
        with _resolves_to("93.184.216.34"), patch(self.POST, return_value=redirect) as post:
            delivered = send_push_to_profile(self.profile.pk, {"title": "Hi"})

        self.assertEqual(post.call_count, 1)
        self.assertEqual(delivered, 0)

    def test_the_post_goes_to_the_address_that_was_checked(self) -> None:
        pins: list[dict[str, str]] = []

        def capture(*_args, **_kwargs):
            pins.append(dict(getattr(_PINS, "map", None) or {}))
            return Mock(status_code=200, is_redirect=False)

        with _resolves_to("93.184.216.34"), patch(self.POST, side_effect=capture):
            send_push_to_profile(self.profile.pk, {"title": "Hi"})

        self.assertEqual(pins, [{"push.example.test": "93.184.216.34"}])
