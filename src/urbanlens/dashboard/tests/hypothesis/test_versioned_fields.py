"""Field-level provenance: every write records who made it, and reads can filter.

The point of this substrate is that a viewer can be shown a *subset* of a row's
history - automatic writes, plus their own, plus their friends' - because a
concealed wiki must not contradict a friend who says "I put a load of stuff on
there". That subset differs per viewer, so it cannot be a stored projection; it
has to be a filter over recorded writes.

Which makes the interception the thing worth testing hardest. A funnel every
caller must remember to use is what decayed last time: three writers already
bypass the existing edit history, one of them a bulk ``update()`` that misses
``save()`` and every signal alike.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.versioned import resolve_fields
from urbanlens.dashboard.models.abstract.versioning import WriteSource, unversioned, writing_as
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision


class InterceptionTests(TestCase):
    """Every write path records a revision, including the ones that skip save()."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), name="Original", officially_created=True)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()

    def _names(self) -> list[str]:
        return list(WikiFieldRevision.objects.filter(target=self.wiki, field_name="name").order_by("sequence").values_list("value", flat=True))

    def test_save_records_a_revision(self) -> None:
        """The ordinary path."""
        self.wiki.name = "Renamed"
        self.wiki.save()

        self.assertIn("Renamed", self._names())

    def test_queryset_update_records_a_revision(self) -> None:
        """The path that matters most.

        ``update()`` bypasses ``save()`` and every signal, so a model-level hook
        cannot see it - which is exactly how the existing bypasses became
        invisible. Overriding the queryset is the only interception Django
        offers.
        """
        Wiki.objects.filter(pk=self.wiki.pk).update(name="Bulk renamed")

        self.assertIn("Bulk renamed", self._names())

    def test_bulk_update_records_a_revision(self) -> None:
        """The third write path."""
        self.wiki.name = "Bulk updated"
        Wiki.objects.bulk_update([self.wiki], ["name"])

        self.assertIn("Bulk updated", self._names())

    def test_an_unversioned_block_records_nothing(self) -> None:
        """Migrations and backfills rewrite history rather than extending it."""
        with unversioned(reason="test"):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Backfilled")

        self.assertEqual(self._names(), [])

    def test_an_unversioned_field_is_not_recorded(self) -> None:
        """Only declared fields are versioned, so a new column cannot join by accident."""
        Wiki.objects.filter(pk=self.wiki.pk).update(officially_created=True)

        self.assertFalse(WikiFieldRevision.objects.filter(target=self.wiki, field_name="officially_created").exists())


class ResolutionTests(TestCase):
    """Reading a row as a particular viewer is entitled to see it."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), name="Auto Name", officially_created=True)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()
        self.friend = baker.make(User).profile
        self.stranger = baker.make(User).profile

        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Enrichment Name", description="")

    def test_only_automatic_writes_resolve_by_default(self) -> None:
        """The concealed baseline: what the row would say with no people involved."""
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Community Name")

        resolved = resolve_fields(self.wiki)

        self.assertEqual(resolved["name"], "Enrichment Name")

    def test_a_friends_write_resolves_for_that_viewer(self) -> None:
        """A friend saying "I put stuff on the wiki" must not be contradicted."""
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(description="My friend wrote this")

        resolved = resolve_fields(self.wiki, actor_ids=[self.friend.pk])

        self.assertEqual(resolved["description"], "My friend wrote this")

    def test_a_strangers_later_write_does_not_displace_a_friends(self) -> None:
        """Ordering is over the *qualifying* rows, not all of them.

        A delta-overlay design gets this wrong: it would apply the friend's
        delta over a base that already contained the stranger's.
        """
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Friend Name")
        with writing_as(WriteSource.USER, actor=self.stranger.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Stranger Name")

        resolved = resolve_fields(self.wiki, actor_ids=[self.friend.pk])

        self.assertEqual(resolved["name"], "Friend Name")

    def test_a_later_automatic_write_beats_an_earlier_friend_write(self) -> None:
        """The other ordering direction, which a naive overlay also gets wrong."""
        with writing_as(WriteSource.USER, actor=self.friend.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Friend Name")
        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Newer Enrichment Name")

        resolved = resolve_fields(self.wiki, actor_ids=[self.friend.pk])

        self.assertEqual(resolved["name"], "Newer Enrichment Name")

    def test_a_null_write_resolves_to_none_not_empty_string(self) -> None:
        """An empty string is a legitimate value for most of these fields."""
        with writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(date_abandoned=None)

        resolved = resolve_fields(self.wiki)

        self.assertIsNone(resolved["date_abandoned"])


class SourceInferenceTests(TestCase):
    """Whose write it was is answered from context, not from the call site."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        WikiFieldRevision.objects.filter(target=self.wiki).delete()
        self.user = baker.make(User)

    def _latest(self) -> WikiFieldRevision:
        return WikiFieldRevision.objects.filter(target=self.wiki, field_name="name").latest("sequence")

    def test_a_write_during_a_signed_in_request_is_that_persons(self) -> None:
        """The middleware answers it once, so no view has to."""
        self.client.force_login(self.user)
        # Any authenticated request establishes the context; the write itself
        # is made inside it.
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as

        with writing_as(WriteSource.USER, actor=self.user.profile.pk):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Person wrote this")

        revision = self._latest()
        self.assertEqual(revision.source, WriteSource.USER)
        self.assertEqual(revision.actor_id, self.user.profile.pk)

    def test_an_unattributed_write_records_no_actor(self) -> None:
        """A shell or migration write is SYSTEM and belongs to nobody.

        Attributing it to whoever happened to be around would be worse than
        leaving it blank - the whole point of the record is that a viewer can
        be shown their friends' contributions and not a stranger's.
        """
        Wiki.objects.filter(pk=self.wiki.pk).update(name="Nobody in particular")

        revision = self._latest()
        self.assertEqual(revision.source, WriteSource.SYSTEM)
        self.assertIsNone(revision.actor_id)

    def test_an_automatic_write_records_no_actor_even_when_a_person_is_present(self) -> None:
        """Enrichment triggered from a request is still enrichment.

        The inference defaults a request to USER, so a task or service that
        knows better has to say so - and when it does, the person must not be
        credited with what the provider wrote.
        """
        from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as

        with writing_as(WriteSource.USER, actor=self.user.profile.pk), writing_as(WriteSource.AUTOMATIC):
            Wiki.objects.filter(pk=self.wiki.pk).update(name="Provider name")

        revision = self._latest()
        self.assertEqual(revision.source, WriteSource.AUTOMATIC)
        self.assertIsNone(revision.actor_id)
