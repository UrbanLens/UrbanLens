"""The external API answers every failure in exactly one envelope.

Before ``errors.ErrorEnvelopeMixin`` was promoted onto the package's two view
bases, three different error shapes went out over the same wire: the
hand-written ``{"error": ...}`` returns, DRF's field-keyed validation dict from
``is_valid(raise_exception=True)``, and DRF's ``{"detail": ...}`` for
everything it raised before a handler ran (401, 403, 404, 405, 429). A
generated client cannot parse three shapes, and *which* one it got depended on
which line of which endpoint happened to fail - so the bug was invisible in
review and only showed up in the client.

These tests hold the contract at the base classes rather than at individual
endpoints, because that is where the guarantee actually lives now. The
important consequence is the ordering one: ``get_exception_handler`` is an
``APIView`` method, so the mixin only wins if it precedes ``APIView`` in the
bases. Reversed, everything still imports, every endpoint still works, and the
envelope silently reverts to DRF's - which is precisely the failure these
assertions exist to catch.

The two probe views below are mounted on a urlconf of their own because no
production endpoint outside ``views_wiki`` raises ``Http404``, and the
anti-enumeration rule ("every 404 renders the byte-identical
``{"error": "Not found."}``, upstream detail discarded") is a promise about the
base class, not about the four wiki views that happen to exercise it today.
The next endpoint to raise ``Http404`` must inherit that behavior without its
author knowing the rule exists.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, ClassVar
from unittest import mock

from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import Http404
from django.test import override_settings
from django.urls import path, reverse
from hypothesis import given
from hypothesis import strategies as st
from model_bakery import baker
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.external_api.errors import INVALID_REQUEST_MESSAGE, NOT_FOUND_BODY, uniform_exception_handler
from urbanlens.dashboard.external_api.mixins import DualAuthJsonView
from urbanlens.dashboard.external_api.throttling import ExternalApiBurstThrottle
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

if TYPE_CHECKING:
    from rest_framework.request import Request

#: A detail message that must never reach the client. Any of these strings
#: appearing in a 404 body would mean the handler forwarded an upstream
#: explanation, turning the endpoint into an existence oracle.
_LEAKY_DETAIL = "pin 'old-mill' exists but belongs to profile 17"


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token.

    Args:
        raw_key: The plaintext key value returned by ``generate_api_key``.

    Returns:
        Extra kwargs for the Django test client.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _CredentialHttp404ProbeView(ExternalApiView):
    """Raises a chatty ``Http404`` from a credential-authenticated endpoint.

    Deliberately attaches ``_LEAKY_DETAIL`` to the exception: a handler that
    forwards DRF's rendered detail instead of substituting the constant body
    would pass a plain ``raise Http404`` test and fail this one.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PROFILE_READ}),
    }

    def get(self, request: Request) -> Response:
        """Always raise ``Http404`` carrying a message that must be discarded.

        Args:
            request: The authenticated request (unused).

        Returns:
            Never returns.

        Raises:
            Http404: Always, with a detail message the envelope must swallow.
        """
        raise Http404(_LEAKY_DETAIL)


class _CredentialNotFoundProbeView(ExternalApiView):
    """Raises DRF's own ``NotFound``, the other route to a 404.

    ``Http404`` and ``NotFound`` arrive at the handler as different exception
    types and only converge because the handler tests for both. A regression
    that dropped one of them from that check would leave half the 404s leaking
    their detail.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PROFILE_READ}),
    }

    def get(self, request: Request) -> Response:
        """Always raise ``NotFound`` carrying a message that must be discarded.

        Args:
            request: The authenticated request (unused).

        Returns:
            Never returns.

        Raises:
            NotFound: Always, with a detail message the envelope must swallow.
        """
        raise NotFound(_LEAKY_DETAIL)


class _DualAuthHttp404ProbeView(DualAuthJsonView):
    """The same 404 probe on the dual-auth base, reachable by a browser session.

    ``DualAuthJsonView`` is the seam the web client shares with the mobile one,
    so it inherits the envelope separately from ``ExternalApiView`` and needs
    its own proof - the two bases have no common ancestor below ``APIView``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PROFILE_READ}),
    }

    def get(self, request: Request) -> Response:
        """Always raise ``Http404`` carrying a message that must be discarded.

        Args:
            request: The session- or credential-authenticated request (unused).

        Returns:
            Never returns.

        Raises:
            Http404: Always, with a detail message the envelope must swallow.
        """
        raise Http404(_LEAKY_DETAIL)


#: The project's real routes plus the probes. The real ones are kept so that
#: ``reverse()`` still resolves inside ``override_settings(ROOT_URLCONF=...)``
#: and so no middleware that reverses a named URL (login redirects, for one)
#: breaks under the swapped urlconf.
urlpatterns = [
    path("__envelope_probe__/http404/", _CredentialHttp404ProbeView.as_view(), name="envelope_probe.http404"),
    path("__envelope_probe__/notfound/", _CredentialNotFoundProbeView.as_view(), name="envelope_probe.notfound"),
    path("__envelope_probe__/dual-auth-http404/", _DualAuthHttp404ProbeView.as_view(), name="envelope_probe.dual_auth_http404"),
    *import_module(django_settings.ROOT_URLCONF).urlpatterns,
]


class _EnvelopeTestCase(TestCase):
    """Shared fixture: a key owner and a broadly-scoped bearer key."""

    #: Every scope the tests in this module exercise. Kept wide on purpose -
    #: these tests are about error *shape*, and a scope denial would mask the
    #: error being asserted with a 403 that also happens to look right.
    SCOPES: ClassVar[list[str]] = [
        ApiKeyScope.PROFILE_READ.value,
        ApiKeyScope.PINS_READ.value,
        ApiKeyScope.PINS_WRITE.value,
        ApiKeyScope.PUSH_MANAGE.value,
    ]

    def setUp(self) -> None:
        """Create the key owner and issue a key carrying :data:`SCOPES`."""
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="envelope-owner")
        self.profile = Profile.objects.get(user=self.user)
        api_key, self.raw_key = generate_api_key(self.user, "Envelope")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=self.SCOPES)

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes*.

        Args:
            scopes: The scope values to grant.

        Returns:
            The raw (plaintext) key value.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def assertEnvelope(self, response, status: int) -> dict:  # noqa: N802 - unittest's own casing
        """Assert *response* is a JSON error in the package's envelope.

        Checks the two halves of the contract that a per-endpoint assertion
        tends to miss: that ``error`` is present *and* that DRF's ``detail``
        key is absent. A handler that merely added ``error`` alongside the
        original body would satisfy the first and fail the second.

        Args:
            response: The test client's response.
            status: The HTTP status code expected.

        Returns:
            The decoded JSON body, for further assertions.
        """
        self.assertEqual(response.status_code, status)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertIsInstance(payload, dict)
        self.assertIn("error", payload)
        self.assertIsInstance(payload["error"], str)
        self.assertNotIn("detail", payload)
        return payload


@override_settings(ROOT_URLCONF=__name__)
class NotFoundEnvelopeTests(_EnvelopeTestCase):
    """Every 404 renders the byte-identical body, whatever raised it."""

    def test_http404_renders_the_constant_body(self) -> None:
        """A bare ``Http404`` from a credential endpoint gives the constant 404."""
        response = self.client.get("/__envelope_probe__/http404/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), NOT_FOUND_BODY)

    def test_drf_not_found_renders_the_constant_body(self) -> None:
        """DRF's ``NotFound`` converges on the same body as ``Http404``."""
        response = self.client.get("/__envelope_probe__/notfound/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), NOT_FOUND_BODY)

    def test_upstream_detail_never_reaches_the_client(self) -> None:
        """The anti-enumeration rule: no part of the raised message survives.

        Asserted against the raw response body rather than the parsed payload,
        so a detail smuggled into a nested key or a header-adjacent field would
        still be caught.
        """
        for url in ("/__envelope_probe__/http404/", "/__envelope_probe__/notfound/"):
            with self.subTest(url=url):
                response = self.client.get(url, **_bearer(self.raw_key))
                self.assertNotIn(b"old-mill", response.content)
                self.assertNotIn(b"profile 17", response.content)

    def test_dual_auth_session_caller_gets_the_same_404(self) -> None:
        """``DualAuthJsonView`` inherits the envelope for the web client too."""
        self.client.force_login(self.user)
        response = self.client.get("/__envelope_probe__/dual-auth-http404/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), NOT_FOUND_BODY)

    def test_dual_auth_credential_caller_gets_the_same_404(self) -> None:
        """And for the credential caller reaching the identical URL."""
        response = self.client.get("/__envelope_probe__/dual-auth-http404/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), NOT_FOUND_BODY)


class ValidationEnvelopeTests(_EnvelopeTestCase):
    """``is_valid(raise_exception=True)`` no longer leaks DRF's field-keyed dict."""

    def test_out_of_range_query_param_is_wrapped_with_its_field(self) -> None:
        """A rejected ``limit`` reports under ``fields``, not at the top level."""
        response = self.client.get(reverse("external_api:pins"), {"limit": 0}, **_bearer(self.raw_key))
        payload = self.assertEnvelope(response, 400)
        self.assertEqual(payload["error"], INVALID_REQUEST_MESSAGE)
        self.assertIn("limit", payload["fields"])
        # The old shape put the field name at the top level; a client keying off
        # that would silently stop finding the messages, so pin it down.
        self.assertNotIn("limit", payload)

    def test_missing_required_field_is_wrapped_with_its_field(self) -> None:
        """A required-field failure on a POST body takes the same shape."""
        response = self.client.post(reverse("external_api:push_devices"), {}, content_type="application/json", **_bearer(self.raw_key))
        payload = self.assertEnvelope(response, 400)
        self.assertEqual(payload["error"], INVALID_REQUEST_MESSAGE)
        self.assertIn("address", payload["fields"])

    def test_field_messages_are_plain_strings(self) -> None:
        """Leaves are ``str``, not DRF ``ErrorDetail``, so the JSON is ordinary.

        ``ErrorDetail`` subclasses ``str`` and serializes identically, so this
        is invisible on the wire - but it is what lets the payload be handed to
        ``json.dumps`` or a non-DRF renderer without surprises.
        """
        response = self.client.post(reverse("external_api:push_devices"), {}, content_type="application/json", **_bearer(self.raw_key))
        messages = response.json()["fields"]["address"]
        self.assertTrue(all(isinstance(message, str) for message in messages))


class DrfRaisedErrorEnvelopeTests(_EnvelopeTestCase):
    """The statuses DRF raises before a handler runs also use the envelope."""

    def test_unauthenticated_request_uses_the_envelope(self) -> None:
        """401 from the authentication layer, not a ``detail`` body."""
        response = self.client.get(reverse("external_api:whoami"))
        self.assertEnvelope(response, 401)

    def test_scope_denial_uses_the_envelope(self) -> None:
        """403 from ``HasApiKeyScope`` - the shape every scope test relies on."""
        raw = self._key_with_scopes([ApiKeyScope.PROFILE_READ.value])
        response = self.client.get(reverse("external_api:pins"), **_bearer(raw))
        self.assertEnvelope(response, 403)

    def test_method_not_allowed_uses_the_envelope(self) -> None:
        """405 names the offending method, in ``error``.

        Probed against ``auth/session/`` rather than an ordinary endpoint
        because a *scoped* view can never actually answer 405: DRF runs
        permissions inside ``initial()``, before it looks up the handler, and
        ``HasApiKeyScope`` fails closed on a method with no scope declaration -
        so an undeclared method is refused as 403 while still on the way in.
        ``auth/session/`` is the one deliberately unscoped view
        (``UnscopedExternalApiView``), which is what lets dispatch get far
        enough to reject the verb itself.
        """
        payload = self.assertEnvelope(self.client.post(reverse("external_api:auth.session"), **_bearer(self.raw_key)), 405)
        self.assertIn("POST", payload["error"])

    def test_dual_auth_method_not_allowed_uses_the_envelope(self) -> None:
        """The same for a real dual-auth endpoint reached by the web client."""
        self.client.force_login(self.user)
        self.assertEnvelope(self.client.get(reverse("e2ee.enroll")), 405)

    def test_throttled_request_uses_the_envelope(self) -> None:
        """429 carries the retry advice in ``error`` rather than ``detail``.

        The burst throttle is squeezed to a single request per hour rather than
        actually making hundreds, and the cache is cleared around the test so
        the counter cannot leak into (or out of) its neighbours.
        """
        cache.clear()
        self.addCleanup(cache.clear)
        # SimpleRateThrottle.__init__ only calls get_rate() when `rate` is
        # falsy, so setting it directly is what makes the override take effect.
        with mock.patch.object(ExternalApiBurstThrottle, "rate", "1/hour", create=True):
            first = self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
            self.assertEqual(first.status_code, 200)
            payload = self.assertEnvelope(self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key)), 429)
        self.assertIn("throttled", payload["error"].lower())


class HandWrittenErrorsAreUnchangedTests(_EnvelopeTestCase):
    """The envelope must not disturb the returns that already used it.

    ``uniform_exception_handler`` only ever sees *exceptions*. A handler that
    returns ``Response({"error": ...}, status=404)`` by hand bypasses it
    entirely and keeps its specific message - which matters, because those
    messages are the ones a client shows the user.
    """

    def test_hand_written_404_keeps_its_own_message(self) -> None:
        """``PinDetailView`` still answers its own "No such pin." wording."""
        response = self.client.get(reverse("external_api:pins.detail", args=["no-such-pin"]), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "No such pin."})


class HandlerPropertyTests(SimpleTestCase):
    """``uniform_exception_handler`` itself, over generated inputs.

    The HTTP tests above prove the handler is *wired up*; these prove it is
    *total*. The envelope's value is that it holds for every error the API can
    produce, which is a universal claim - and the endpoint tests can only ever
    sample the handful of exceptions today's endpoints happen to raise. A
    future serializer with an unusual error structure is exactly the case a
    fixed example misses.

    No database is touched, so these are cheap enough to run over hundreds of
    generated structures.
    """

    #: Field names shaped like real serializer fields. Generated rather than
    #: fixed so a handler that special-cased particular keys would be caught.
    _FIELD_NAMES = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True)

    @given(message=st.text(max_size=200))
    def test_every_http404_message_collapses_to_one_body(self, message: str) -> None:
        """The anti-enumeration rule as a universal claim, not a sample.

        Whatever an upstream helper attaches to its ``Http404`` - a slug, an
        owner id, a reason - the caller sees the same constant. Any dependence
        of the body on the message would make the endpoint an oracle, and this
        is the assertion that rules out *all* of them rather than the two
        phrasings a fixed test happens to try.
        """
        response = uniform_exception_handler(Http404(message), {})
        assert response is not None
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, NOT_FOUND_BODY)

    @given(message=st.text(max_size=200))
    def test_every_not_found_message_collapses_to_one_body(self, message: str) -> None:
        """DRF's ``NotFound`` is held to the identical constant."""
        response = uniform_exception_handler(NotFound(message), {})
        assert response is not None
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, NOT_FOUND_BODY)

    @given(
        errors=st.dictionaries(
            keys=_FIELD_NAMES,
            values=st.lists(st.text(min_size=1, max_size=60), min_size=1, max_size=3),
            min_size=1,
            max_size=5,
        )
    )
    def test_validation_errors_always_land_under_fields(self, errors: dict[str, list[str]]) -> None:
        """Any field-keyed validation structure keeps its keys - one level down.

        The two halves that matter to a client: nothing is dropped (every
        submitted field name still appears), and nothing is promoted (the field
        names live under ``fields``, never at the top level where they would
        collide with ``error`` itself).
        """
        response = uniform_exception_handler(ValidationError(errors), {})
        assert response is not None
        self.assertEqual(response.status_code, 400)
        payload = response.data
        self.assertEqual(payload["error"], INVALID_REQUEST_MESSAGE)
        self.assertEqual(set(payload["fields"]), set(errors))
        self.assertNotIn("detail", payload)
        for name in errors:
            self.assertNotIn(name, payload)

    @given(
        errors=st.dictionaries(
            keys=_FIELD_NAMES,
            values=st.lists(st.text(min_size=1, max_size=60), min_size=1, max_size=3),
            min_size=1,
            max_size=5,
        )
    )
    def test_validation_leaves_are_always_plain_strings(self, errors: dict[str, list[str]]) -> None:
        """No ``ErrorDetail`` survives, however deeply nested the structure is."""
        response = uniform_exception_handler(ValidationError(errors), {})
        assert response is not None
        for messages in response.data["fields"].values():
            for message in messages:
                # ErrorDetail subclasses str, so identity of type is the check.
                self.assertIs(type(message), str)

    @given(message=st.text(min_size=1, max_size=200))
    def test_single_message_exceptions_always_use_the_error_key(self, message: str) -> None:
        """``{"detail": x}`` becomes ``{"error": x}`` with the text preserved.

        Unlike a 404, these messages *are* meant to reach the caller - a
        throttle's retry advice or a permission refusal is actionable. Only the
        key changes.
        """
        response = uniform_exception_handler(PermissionDenied(message), {})
        assert response is not None
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"error": message})
