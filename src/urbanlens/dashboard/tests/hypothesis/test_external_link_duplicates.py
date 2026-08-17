"""Auto-discovered links are unique per (pin, url) / (wiki, url), enforced by the DB.

``add_pin_link``/``add_wiki_link`` run from a ``LocationCache`` post-save signal,
and cache rows are written by panel fetches - which have their own Celery queue
at concurrency 20. Two panels that surface the same URL for one pin can both pass
the exists() fast path, so the unique constraint added in migration 0047 is what
actually decides; the loser's insert is absorbed rather than escaping as a 500
from inside a signal handler.

The constraint hashes the URL (``UniqueConstraint(F(owner), MD5("url"))``)
because ``url`` holds up to 2000 characters and a plain btree entry over that in
multibyte UTF-8 can exceed Postgres' row limit.
"""

from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, transaction
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.links.queryset import LinkQuerySet
from urbanlens.dashboard.services.locations.external_links import add_pin_link, add_wiki_link

_URL = "https://example.org/history"


class DuplicateExternalLinkTests(TestCase):
    def setUp(self) -> None:
        self.pin = baker.make("dashboard.Pin")
        self.wiki = baker.make("dashboard.Wiki")

    def test_the_database_refuses_a_duplicate_pin_link(self) -> None:
        """The constraint, not the caller, is what makes duplicates impossible."""
        PinLink.objects.create(pin=self.pin, url=_URL, name="one")

        with pytest.raises(IntegrityError), transaction.atomic():
            PinLink.objects.create(pin=self.pin, url=_URL, name="two")

    def test_the_database_refuses_a_duplicate_wiki_link(self) -> None:
        WikiLink.objects.create(wiki=self.wiki, url=_URL, name="one")

        with pytest.raises(IntegrityError), transaction.atomic():
            WikiLink.objects.create(wiki=self.wiki, url=_URL, name="two")

    def test_the_same_url_on_a_different_pin_is_allowed(self) -> None:
        """Uniqueness is per owner - two users may link the same page."""
        other_pin = baker.make("dashboard.Pin")
        PinLink.objects.create(pin=self.pin, url=_URL, name="one")

        PinLink.objects.create(pin=other_pin, url=_URL, name="one")

        self.assertEqual(PinLink.objects.filter(url=_URL).count(), 2)

    def test_a_racing_add_is_absorbed_rather_than_raising(self) -> None:
        """The loser of the exists()-then-create race returns False, not a 500.

        A concurrent panel inserts between the fast-path check and this call's
        insert. Neutering the check reproduces that ordering deterministically:
        the create then hits the constraint, which must be swallowed - this runs
        inside a signal handler on a Celery queue, where an IntegrityError would
        surface as a task failure.
        """
        PinLink.objects.create(pin=self.pin, url=_URL, name="the winner")

        with mock.patch.object(LinkQuerySet, "exists", return_value=False):
            self.assertFalse(add_pin_link(self.pin, _URL, "the loser"))

        self.assertEqual(PinLink.objects.filter(pin=self.pin, url=_URL).count(), 1)
        self.assertEqual(PinLink.objects.get(pin=self.pin, url=_URL).name, "the winner")

    def test_first_add_still_creates(self) -> None:
        self.assertTrue(add_pin_link(self.pin, _URL, "official site"))

        link = PinLink.objects.get(pin=self.pin, url=_URL)
        self.assertEqual(link.name, "official site")

    def test_second_add_is_a_no_op(self) -> None:
        add_pin_link(self.pin, _URL, "official site")

        self.assertFalse(add_pin_link(self.pin, _URL, "a different name"))
        self.assertEqual(PinLink.objects.filter(pin=self.pin, url=_URL).count(), 1)

    def test_wiki_link_add_is_idempotent(self) -> None:
        self.assertTrue(add_wiki_link(self.wiki, _URL, "official site"))

        self.assertFalse(add_wiki_link(self.wiki, _URL, "a different name"))
        self.assertEqual(WikiLink.objects.filter(wiki=self.wiki, url=_URL).count(), 1)
