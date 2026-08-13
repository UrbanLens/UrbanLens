"""Adding an auto-discovered link must survive duplicates already in the table.

``add_pin_link``/``add_wiki_link`` run from a ``LocationCache`` post-save signal,
and cache rows are written by panel fetches - which have their own Celery queue
at concurrency 20. Two panels that surface the same URL for one pin can both
miss and both insert, because there is no unique constraint on ``(pin, url)``.

The duplicate row itself is harmless. What was not harmless is that
``get_or_create`` raises ``MultipleObjectsReturned`` on *every* subsequent call
for that URL once two rows exist - so a one-off race turned into a permanent
exception inside a signal handler, on a task queue, for that pin.

Closing the race needs a unique constraint, which is entangled with the
user-facing "add a link" flow (see docs/PROBLEMS.md). Not raising is
independent of that, and is what these cover.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.services.locations.external_links import add_pin_link, add_wiki_link

_URL = "https://example.org/history"


class DuplicateExternalLinkTests(TestCase):
    def setUp(self) -> None:
        self.pin = baker.make("dashboard.Pin")
        self.wiki = baker.make("dashboard.Wiki")

    def test_pin_link_survives_a_pre_existing_duplicate_pair(self) -> None:
        PinLink.objects.create(pin=self.pin, url=_URL, name="one")
        PinLink.objects.create(pin=self.pin, url=_URL, name="two")

        self.assertFalse(add_pin_link(self.pin, _URL, "three"))
        self.assertEqual(PinLink.objects.filter(pin=self.pin, url=_URL).count(), 2)

    def test_wiki_link_survives_a_pre_existing_duplicate_pair(self) -> None:
        WikiLink.objects.create(wiki=self.wiki, url=_URL, name="one")
        WikiLink.objects.create(wiki=self.wiki, url=_URL, name="two")

        self.assertFalse(add_wiki_link(self.wiki, _URL, "three"))
        self.assertEqual(WikiLink.objects.filter(wiki=self.wiki, url=_URL).count(), 2)

    def test_first_add_still_creates(self) -> None:
        self.assertTrue(add_pin_link(self.pin, _URL, "official site"))

        link = PinLink.objects.get(pin=self.pin, url=_URL)
        self.assertEqual(link.name, "official site")

    def test_second_add_is_a_no_op(self) -> None:
        add_pin_link(self.pin, _URL, "official site")

        self.assertFalse(add_pin_link(self.pin, _URL, "a different name"))
        self.assertEqual(PinLink.objects.filter(pin=self.pin, url=_URL).count(), 1)
