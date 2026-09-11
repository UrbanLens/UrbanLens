"""Adding one alias to a community wiki must not scale with who else pinned the place.

A wiki belongs to a location, and a location has one pin per person who
saved it. ``sync_wiki_alias_to_pins`` mirrored a new wiki alias onto every
opted-in one of those pins with a ``get_or_create`` each, inside the
committing request - and each of those writes fires two further receivers
(the pin->wiki mirror, and the name-sensitive cache invalidation). So the
cost of one person adding a name to a popular place was set by how many
*other* people had pinned it, which is the shape the availability work
exists to remove (N21 H25).

The fan-out itself is still correct and still has to happen. What changes is
where: a bulk task, on the queue whose whole purpose is work whose size is
set by how much somebody owns.
"""

from __future__ import annotations

from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki


class _FanOutCase(TestCase):
    """A location that several profiles have pinned, all opted in to the sync."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)

    def _pin_the_location(self, count: int) -> list[Pin]:
        pins = []
        for _ in range(count):
            user = baker.make("auth.User")
            profile = Profile.objects.get(user=user)
            Profile.objects.filter(pk=profile.pk).update(sync_aliases=SyncAliasesDirection.BOTH)
            pins.append(baker.make(Pin, profile=profile, location=self.location))
        return pins

    def _add_a_wiki_alias(self, name: str) -> tuple[WikiAlias, int]:
        """Add one alias the way a request does, returning it and the query count.

        The capture has to be the *outer* context: context managers exit in
        reverse, so with it inside, the on-commit callbacks - which are the
        whole point - run after it has stopped counting, and the test passes
        against any implementation at all.
        """
        with CaptureQueriesContext(connection) as queries, self.captureOnCommitCallbacks(execute=True):
            alias = WikiAlias.objects.create(wiki=self.wiki, name=name, kind=AliasType.OFFICIAL)
        return alias, len(queries)


class TheRequestDoesNotWalkEveryOtherProfilesPinTests(_FanOutCase):
    """The committing request's cost must not be set by the crowd at that place."""

    def test_the_query_count_does_not_grow_with_the_number_of_other_pins(self) -> None:
        self._pin_the_location(2)
        _, small = self._add_a_wiki_alias("Small Crowd Mill")

        self._pin_the_location(10)
        _, large = self._add_a_wiki_alias("Large Crowd Mill")

        self.assertEqual(
            large,
            small,
            f"adding an alias cost {small} queries with 2 pins at the location and {large} with 12, "
            "so the person adding a name pays for everyone else who pinned the place",
        )

    def test_the_fan_out_is_handed_to_the_bulk_queue(self) -> None:
        from urbanlens.dashboard.services.sandbox.queues import Queue
        from urbanlens.dashboard.tasks import fan_out_wiki_alias_to_pins

        self._pin_the_location(2)

        with mock.patch("urbanlens.dashboard.models.aliases.signals.safely_enqueue_task") as enqueue:
            alias, _ = self._add_a_wiki_alias("Handed Off Mill")

        enqueue.assert_called_once_with(fan_out_wiki_alias_to_pins, alias.pk)
        self.assertEqual(fan_out_wiki_alias_to_pins.queue, Queue.BULK)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TheFanOutStillHappensTests(_FanOutCase):
    """The half that stops the tests above passing against a deleted feature.

    Eager, so the task the signal now hands off to actually runs - which also
    makes this the test that the hand-off is wired to a task that works, not
    just to a name.
    """

    def test_every_opted_in_pin_gets_the_alias(self) -> None:
        pins = self._pin_the_location(3)

        alias, _ = self._add_a_wiki_alias("Reached Everyone Mill")

        for pin in pins:
            self.assertTrue(
                PinAlias.objects.filter(pin=pin, name="Reached Everyone Mill").exists(),
                f"pin {pin.pk} never received the wiki alias",
            )
        self.assertEqual(alias.name, "Reached Everyone Mill")

    def test_a_profile_that_opted_out_is_left_alone(self) -> None:
        pins = self._pin_the_location(1)
        Profile.objects.filter(pk=pins[0].profile_id).update(sync_aliases=SyncAliasesDirection.OFF)

        self._add_a_wiki_alias("Not Wanted Mill")

        self.assertFalse(PinAlias.objects.filter(pin=pins[0], name="Not Wanted Mill").exists())
