"""Deleting a pin while a background task writes to it neither fails the delete nor leaves the write behind.

Django collects a pin's children in Python and the foreign keys are checked at commit, so a child committed between
the collect and the commit made the delete fail: on the v0.8.0 location run, deleting a pin two seconds after
creating it answered 500 while the new-pin Wikipedia seed wrote its article, and again while it wrote the revision.
Each test starts the other writer from ``pre_delete``, after the collect, on its own connection.
"""

from __future__ import annotations

from collections.abc import Callable
import threading

from django.contrib.auth.models import User
from django.db import connection
from django.db.models.signals import pre_delete
from django.test import TransactionTestCase
from model_bakery import baker

from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.pin_edit import delete_pin
from urbanlens.dashboard.services.wiki.articles import save_article

#: How long the delete waits on the other writer before carrying on; the fix makes that writer wait for the delete.
_WRITER_GRACE_SECONDS = 3


class PinDeleteRacesChildWritesTests(TransactionTestCase):
    def setUp(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.pin = baker.make(Pin, profile=baker.make(User).profile)

    def _delete_while(self, write: Callable[[], object]) -> object:
        outcome: dict[str, object] = {}

        def writer() -> None:
            try:
                outcome["write"] = write()
            except Exception as exc:  # noqa: BLE001 - the outcome is what the test reads
                outcome["write"] = exc
            finally:
                connection.close()

        threads: list[threading.Thread] = []

        def start_writer(sender, instance, **kwargs) -> None:
            if instance.pk == self.pin.pk and not threads:
                threads.append(threading.Thread(target=writer))
                threads[0].start()
                threads[0].join(timeout=_WRITER_GRACE_SECONDS)

        pre_delete.connect(start_writer, sender=Pin, weak=False)
        try:
            delete_pin(Pin.objects.get(pk=self.pin.pk))
        finally:
            pre_delete.disconnect(start_writer, sender=Pin)
            for thread in threads:
                thread.join(timeout=30)
        self.assertTrue(threads, "the delete never reached the pin")
        return outcome.get("write")

    def test_an_article_written_during_the_delete(self) -> None:
        self._delete_while(lambda: Article.objects.create(pin_id=self.pin.pk, content="seeded"))

        self.assertFalse(Pin.objects.filter(pk=self.pin.pk).exists())
        self.assertFalse(Article.objects.filter(pin_id=self.pin.pk).exists())

    def test_a_revision_written_during_the_delete(self) -> None:
        article, _revision = save_article(editor=None, content="first", pin=self.pin)

        written = self._delete_while(lambda: save_article(editor=None, content="second", pin=self.pin))

        self.assertIsInstance(written, Pin.DoesNotExist, "the late write should find the pin gone")
        self.assertFalse(Pin.objects.filter(pk=self.pin.pk).exists())
        self.assertFalse(ArticleRevision.objects.filter(article_id=article.pk).exists())
