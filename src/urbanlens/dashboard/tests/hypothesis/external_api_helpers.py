"""Shared fixture for external API write-route tests: an owner and a stranger, each holding a key."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

if TYPE_CHECKING:
    from django.http import HttpResponse

#: Valid JSON nested past the interpreter's recursion limit: ``json.loads`` raises ``RecursionError``, not a
#: ``ValueError``, on it.
DEEPLY_NESTED_JSON = "[" * 100_000 + "]" * 100_000

#: JSON that parses to something other than an object, and bodies that do not parse at all.
MALFORMED_JSON_BODIES = ("[]", "null", '"text"', "{", "[1,", DEEPLY_NESTED_JSON)


class ExternalApiRouteCase(TestCase):
    """An owner and a stranger, each with a key carrying ``scopes``, plus a read-only key for the owner."""

    #: The scopes every key in the fixture carries.
    scopes: ClassVar[tuple[ApiKeyScope, ...]] = ()
    #: The subset a read-only key carries; empty means a key with no scopes at all.
    read_scopes: ClassVar[tuple[ApiKeyScope, ...]] = ()

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile
        self.auth = self.key(self.owner_user, self.scopes)
        self.stranger_auth = self.key(self.stranger_user, self.scopes)
        self.read_only = self.key(self.owner_user, self.read_scopes)

    @staticmethod
    def key(user: User, scopes: tuple[ApiKeyScope, ...]) -> dict[str, str]:
        """Issue a key for *user* carrying exactly *scopes*.

        Args:
            user: The key's owner.
            scopes: The scopes to grant.

        Returns:
            Request kwargs carrying the bearer header.
        """
        api_key, raw = generate_api_key(user, "Write route client")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[scope.value for scope in scopes])
        return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def send(self, method: str, url: str, body: object = None, *, auth: dict[str, str] | None = None) -> HttpResponse:
        """Send *body* as JSON with *auth*, or anonymously when *auth* is empty.

        Args:
            method: ``post``, ``put``, ``patch`` or ``delete``.
            url: The route.
            body: Serialized as JSON; a ``str`` is sent raw.
            auth: Request kwargs from :meth:`key`; ``None`` uses the owner's key.

        Returns:
            The response.
        """
        kwargs = self.auth if auth is None else auth
        if body is None:
            return getattr(self.client, method)(url, **kwargs)
        return getattr(self.client, method)(url, data=body, content_type="application/json", **kwargs)

    def assert_refused_without_credentials(self, method: str, url: str, body: object = None) -> None:
        """Anonymous is 401/403, and a key without the write scope is 403."""
        self.assertIn(self.send(method, url, body, auth={}).status_code, (401, 403))
        self.assertEqual(self.send(method, url, body, auth=self.read_only).status_code, 403)

    def assert_malformed_bodies_are_4xx(self, method: str, url: str) -> None:
        """Every non-object or unparseable JSON body is answered 4xx, never 2xx or 5xx."""
        for raw in MALFORMED_JSON_BODIES:
            with self.subTest(body=raw[:20]):
                status = self.send(method, url, raw).status_code
                self.assertGreaterEqual(status, 400, raw[:20])
                self.assertLess(status, 500, raw[:20])
