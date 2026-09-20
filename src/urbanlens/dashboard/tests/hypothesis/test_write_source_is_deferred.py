"""Naming the writer of a request's writes costs a query, so it waits until there is a write.

``WriteSourceMiddleware`` binds the signed-in profile for every request. Resolving that profile up
front charged a ``dashboard_profiles`` query to requests that write nothing - which is nearly all of
them, and is two dozen per map load once basemap tiles are counted. The provenance itself is not
negotiable, so what is tested here is that deferring the lookup changes only when it happens.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.middleware import WriteSourceMiddleware
from urbanlens.dashboard.models.abstract.versioning import (
    WriteSource,
    current_write_actor,
    current_write_source,
    writing_as,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision


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
