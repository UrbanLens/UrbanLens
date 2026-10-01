"""Naming the writer of a request's writes costs a query, so it waits until there is a write.

``WriteSourceMiddleware`` binds the signed-in profile for every request. Resolving that profile up
front charged a ``dashboard_profiles`` query to requests that write nothing - which is nearly all of
them, and is two dozen per map load once basemap tiles are counted. The provenance itself is not
negotiable, so what is tested here is that deferring the lookup changes only when it happens.
"""

from __future__ import annotations

from datetime import timedelta
import pathlib
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.middleware import WriteSourceMiddleware
from urbanlens.dashboard.models.abstract.versioning import (
    WriteSource,
    current_write_actor,
    current_write_source,
    request_writer,
    writing_as,
)
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

AccessToken = get_access_token_model()

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _picks_a_source_from_auth(path: pathlib.Path) -> bool:
    """Whether a module decides a :class:`WriteSource` from the request's own authentication."""
    text = path.read_text()
    return "WriteSource.USER" in text and "is_authenticated" in text


class DeferredActorTests(TestCase):
    """A callable actor is resolved at the first write, and only then."""

    def test_an_actor_nobody_asks_for_is_never_resolved(self) -> None:
        """The whole point: a request that writes nothing does not look up who would have written."""
        asked = 0

        def actor() -> int | None:
            nonlocal asked
            asked += 1
            return 7

        with writing_as(WriteSource.USER, actor=actor):
            pass

        self.assertEqual(asked, 0)

    def test_a_write_still_knows_who_made_it(self) -> None:
        """Deferring must not lose the attribution, which is the reason the binding exists."""
        with writing_as(WriteSource.USER, actor=lambda: 7):
            self.assertEqual(current_write_actor(), 7)
            self.assertEqual(current_write_source(), WriteSource.USER)

    def test_the_lookup_happens_once_however_many_writes_follow(self) -> None:
        """A request that writes a hundred rows must not cost a hundred profile queries."""
        asked = 0

        def actor() -> int | None:
            nonlocal asked
            asked += 1
            return 7

        with writing_as(WriteSource.USER, actor=actor):
            for _ in range(5):
                self.assertEqual(current_write_actor(), 7)

        self.assertEqual(asked, 1)

    def test_an_actor_resolved_inside_a_block_does_not_outlive_it(self) -> None:
        """Memoising writes to the same context variable the block restores, so it has to restore it."""
        with writing_as(WriteSource.USER, actor=lambda: 7):
            self.assertEqual(current_write_actor(), 7)

        self.assertIsNone(current_write_actor())


class MiddlewareAttributionTests(TestCase):
    """What the middleware binds, seen from the write rather than from the binding."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.wiki = baker.make(Wiki, location=baker.make(Location), name="Original")
        WikiFieldRevision.objects.filter(target=self.wiki).delete()

    def _run(self, user: User | AnonymousUser, write: bool) -> None:
        """Send one request through the middleware, optionally writing during it."""
        request = RequestFactory().get("/anything/")
        request.user = user

        def view(_request: object) -> object:
            if write:
                self.wiki.name = "Renamed"
                self.wiki.save()
            return object()

        WriteSourceMiddleware(view)(request)  # type: ignore[arg-type]

    def test_a_signed_in_request_that_writes_nothing_asks_for_no_profile(self) -> None:
        """One query for `auth_user`, none for the profile row - the saving this change exists for."""
        with self.assertNumQueries(0):
            self._run(self.user, write=False)

    def test_a_write_is_attributed_to_the_profile_that_made_it(self) -> None:
        """Unchanged behaviour, now reached the long way round."""
        self._run(self.user, write=True)

        revision = WikiFieldRevision.objects.filter(target=self.wiki, field_name="name").latest("pk")
        self.assertEqual(revision.source, WriteSource.USER)
        self.assertEqual(revision.actor_id, self.user.profile.pk)

    def test_an_anonymous_write_is_the_system(self) -> None:
        """Nobody to attribute it to, and no lookup to defer."""
        self._run(AnonymousUser(), write=True)

        revision = WikiFieldRevision.objects.filter(target=self.wiki, field_name="name").latest("pk")
        self.assertEqual(revision.source, WriteSource.SYSTEM)
        self.assertIsNone(revision.actor_id)


class ExternalApiAttributionTests(TestCase):
    """The API binds its own source, so it defers the same lookup or the saving stops at the door.

    ``ExternalApiView.initial()`` cannot use ``WriteSourceMiddleware`` - it needs the caller DRF
    authenticated, which happens after the middleware has run - so it is a second implementation of
    the same rule and drifted from it once already.

    Unlike the middleware, this saves no query on any endpoint measured, and the audit finding that
    asked for it assumed it would. ``ApiKeyAuthentication`` resolves its key with
    ``select_related("user", "user__profile")``, so the profile is already in memory there; on the
    OAuth2 path (``select_related("application", "user")``, no profile) the view reads
    ``request.user.profile`` for its own filtering, and either way the request pays exactly one
    ``dashboard_profiles`` query with the binding eager or lazy. What is worth holding is that the
    two implementations of one rule now say the same thing - they had already drifted once - so
    what is asserted is the shape of the binding rather than a saving that is not there.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        _key, self.raw_key = generate_api_key(self.user, "Attribution client")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[ApiKeyScope.LISTS_READ.value, ApiKeyScope.LISTS_WRITE.value]
        )

    def _get(self, path: str):
        return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")

    def test_a_read_resolves_the_profile_once_however_it_authenticated(self) -> None:
        """The ceiling the binding must not raise, on both authenticators.

        Written as "once", not "never", because measurement said so: the number is what the
        authenticator and the view need between them, and a binding that resolved its own copy
        would make it two.
        """
        token = AccessToken.objects.create(
            user=self.user,
            application=first_party_application(),
            token="write-source-deferral-token",
            expires=timezone.now() + timedelta(hours=1),
            scope=f"{ApiKeyScope.LISTS_READ.value} {ApiKeyScope.LISTS_WRITE.value}",
        )
        credentials = {"api key": f"Bearer {self.raw_key}", "oauth2": f"Bearer {token.token}"}

        for kind, header in credentials.items():
            with self.subTest(credential=kind):
                self.client.get(
                    "/dashboard/api/external/v1/lists/", HTTP_AUTHORIZATION=header
                )  # warms the first-request caches

                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get("/dashboard/api/external/v1/lists/", HTTP_AUTHORIZATION=header)

                self.assertEqual(response.status_code, 200)
                profile_reads = [
                    query["sql"] for query in queries.captured_queries if "dashboard_profiles" in query["sql"]
                ]
                self.assertLessEqual(
                    len(profile_reads), 1, f"{kind}: the writer's profile was resolved {len(profile_reads)} times"
                )

    def test_what_it_binds_still_names_the_caller_when_something_asks(self) -> None:
        """Deferring the lookup must not lose it.

        Asserted on the binding rather than on a revision row: nothing the external API writes is a
        versioned model, so a write through it would prove the attribution reached nowhere.
        """
        bound: dict[str, object] = {}

        def record(source: object, *, actor: object = None) -> None:
            bound["source"] = source
            bound["actor"] = actor

        with mock.patch("urbanlens.dashboard.models.abstract.versioning.bind_write_source", record):
            self.assertEqual(self._get("/dashboard/api/external/v1/lists/").status_code, 200)

        source, actor = bound["source"], bound["actor"]
        self.assertTrue(callable(source), "bound eagerly, so every request pays for the lookup again")
        self.assertTrue(callable(actor))
        self.assertEqual(source(), WriteSource.USER)  # type: ignore[operator]
        self.assertEqual(actor(), self.user.profile.pk)  # type: ignore[operator]


class OneRuleDecidesTheWriterTests(TestCase):
    """The rule naming a request's writer has one definition, and every binder reaches for it.

    N26 #10: the site and the API each held their own copy, they drifted apart once, and nothing
    failed when they did - a write attributed to SYSTEM that a user actually made is a row that
    looks correct everywhere except in what it means. No test of either path catches that, because
    each path is self-consistent; what catches it is there being one rule to test.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)

    def test_nothing_outside_versioning_decides_whether_a_request_has_a_writer(self) -> None:
        """A new entry point spelling the rule out again is the failure this exists to name."""
        versioning = REPO_ROOT / "src/urbanlens/dashboard/models/abstract/versioning.py"
        forked = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "src/urbanlens").rglob("*.py")
            if path != versioning and "/tests/" not in path.as_posix() and _picks_a_source_from_auth(path)
        ]
        self.assertEqual(
            forked,
            [],
            f"{forked} decide a WriteSource from is_authenticated themselves; call request_writer() instead",
        )

    def test_it_names_the_signed_in_profile(self) -> None:
        request = RequestFactory().get("/anything/")
        request.user = self.user

        source, actor = request_writer(request)

        self.assertEqual(source(), WriteSource.USER)
        self.assertEqual(actor(), self.user.profile.pk)

    def test_an_anonymous_request_is_the_system_with_nobody_to_name(self) -> None:
        request = RequestFactory().get("/anything/")
        request.user = AnonymousUser()

        source, actor = request_writer(request)

        self.assertEqual(source(), WriteSource.SYSTEM)
        self.assertIsNone(actor())

    def test_a_request_that_never_authenticated_is_the_system_rather_than_an_error(self) -> None:
        """Both binders run before some of the stack that sets `user`, and a 500 here is a 500 everywhere."""
        source, actor = request_writer(RequestFactory().get("/anything/"))

        self.assertEqual(source(), WriteSource.SYSTEM)
        self.assertIsNone(actor())

    def test_it_reads_nothing_until_asked(self) -> None:
        """The deferral is the point; building the pair must not touch the database."""
        request = RequestFactory().get("/anything/")
        request.user = self.user

        with self.assertNumQueries(0):
            request_writer(request)
