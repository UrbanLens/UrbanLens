"""Pin and wiki subtree/ancestor expansion runs one query whatever the tree's depth."""

from __future__ import annotations

from itertools import count

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.handlers.wiki import with_wiki_descendants

_coords = count(1)


def _location() -> Location:
    step = next(_coords)
    return baker.make(Location, latitude=40.0 + step * 0.001, longitude=-75.0 - step * 0.001)


class PinTreeClosureTests(TestCase):
    def setUp(self) -> None:
        self.profile = User.objects.create_user(username="tree", password="pw").profile

    def _chain(self, depth: int) -> list[Pin]:
        pins = [baker.make(Pin, profile=self.profile, location=_location())]
        for _ in range(depth):
            pins.append(baker.make(Pin, profile=self.profile, location=_location(), parent_pin=pins[-1]))
        return pins

    def _queries(self, action) -> int:
        with CaptureQueriesContext(connection) as ctx:
            action()
        return len(ctx.captured_queries)

    def test_subtree_is_one_query_at_any_depth(self) -> None:
        shallow = self._chain(2)
        deep = self._chain(15)
        self.assertEqual(self._queries(lambda: list(Pin.objects.filter(pk=shallow[0].pk).with_descendants())), 1)
        self.assertEqual(self._queries(lambda: list(Pin.objects.filter(pk=deep[0].pk).with_descendants())), 1)
        self.assertEqual(
            set(Pin.objects.filter(pk=deep[0].pk).with_descendants().values_list("pk", flat=True)),
            {pin.pk for pin in deep},
        )

    def test_subtree_stays_composable(self) -> None:
        chain = self._chain(3)
        subtree = Pin.objects.filter(pk=chain[1].pk).with_descendants()
        self.assertEqual(set(subtree.exclude(pk=chain[1].pk).values_list("pk", flat=True)), {chain[2].pk, chain[3].pk})
        self.assertTrue(subtree.filter(pk=chain[3].pk).exists())
        self.assertFalse(subtree.filter(pk=chain[0].pk).exists())

    def test_seed_of_several_roots_and_a_sliced_seed(self) -> None:
        first, second = self._chain(1), self._chain(1)
        both = Pin.objects.filter(pk__in=[first[0].pk, second[0].pk]).with_descendants()
        self.assertEqual(set(both.values_list("pk", flat=True)), {pin.pk for pin in first + second})
        sliced = Pin.objects.filter(pk=first[0].pk).order_by("pk")[:1].with_descendants()
        self.assertEqual(set(sliced.values_list("pk", flat=True)), {pin.pk for pin in first})

    def test_a_cycle_terminates(self) -> None:
        chain = self._chain(2)
        Pin.objects.filter(pk=chain[0].pk).update(parent_pin_id=chain[2].pk)
        self.assertEqual(
            set(Pin.objects.filter(pk=chain[0].pk).with_descendants().values_list("pk", flat=True)),
            {pin.pk for pin in chain},
        )
        self.assertEqual(
            set(Pin.objects.filter(pk=chain[0].pk).with_ancestors().values_list("pk", flat=True)),
            {pin.pk for pin in chain},
        )
        chain[2].refresh_from_db()
        self.assertEqual([pin.pk for pin in chain[2].ancestor_chain()], [chain[1].pk, chain[0].pk])

    def test_a_self_parented_row_terminates(self) -> None:
        chain = self._chain(1)
        Pin.objects.filter(pk=chain[1].pk).update(parent_pin_id=chain[1].pk)
        self.assertEqual(
            set(Pin.objects.filter(pk=chain[1].pk).with_descendants().values_list("pk", flat=True)), {chain[1].pk}
        )

    def test_ancestor_chain_is_ordered_and_one_query_at_any_depth(self) -> None:
        deep = self._chain(15)
        leaf = Pin.objects.get(pk=deep[-1].pk)
        with CaptureQueriesContext(connection) as ctx:
            chain = leaf.ancestor_chain()
            _ = [ancestor.location.latitude for ancestor in chain]
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual([pin.pk for pin in chain], [pin.pk for pin in reversed(deep[:-1])])
        self.assertEqual(deep[0].ancestor_chain(), [])

    def test_would_create_cycle_is_one_query_at_any_depth(self) -> None:
        deep = self._chain(15)
        root, leaf = Pin.objects.get(pk=deep[0].pk), Pin.objects.get(pk=deep[-1].pk)
        with CaptureQueriesContext(connection) as ctx:
            self.assertTrue(root.would_create_cycle(leaf))
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertFalse(leaf.would_create_cycle(root))
        self.assertTrue(root.would_create_cycle(root))
        self.assertFalse(root.would_create_cycle(None))
        other = self._chain(0)[0]
        self.assertFalse(root.would_create_cycle(other))

    def test_lineage_of_a_root_costs_no_query(self) -> None:
        root = self._chain(0)[0]
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(Pin.objects.lineage_ids(root), {root.pk})
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_lineage_of_a_nested_pin_is_one_query(self) -> None:
        deep = self._chain(10)
        with CaptureQueriesContext(connection) as ctx:
            lineage = Pin.objects.lineage_ids(deep[-1])
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual(lineage, {pin.pk for pin in deep})


class WikiTreeClosureTests(TestCase):
    def _chain(self, depth: int) -> list[Wiki]:
        wikis = [baker.make(Wiki, location=_location())]
        for _ in range(depth):
            wikis.append(baker.make(Wiki, location=_location(), parent_wiki=wikis[-1]))
        return wikis

    def test_subtree_is_one_query_at_any_depth(self) -> None:
        for depth in (2, 12):
            chain = self._chain(depth)
            with CaptureQueriesContext(connection) as ctx:
                found = {wiki.pk for wiki in Wiki.objects.filter(pk=chain[0].pk).with_descendants()}
            self.assertEqual(len(ctx.captured_queries), 1)
            self.assertEqual(found, {wiki.pk for wiki in chain})

    def test_undo_expansion_uses_the_same_closure(self) -> None:
        chain = self._chain(12)
        with CaptureQueriesContext(connection) as ctx:
            found = with_wiki_descendants([chain[3]])
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual({wiki.pk for wiki in found}, {wiki.pk for wiki in chain[3:]})

    def test_ancestors_and_cycle_check(self) -> None:
        chain = self._chain(12)
        leaf = Wiki.objects.get(pk=chain[-1].pk)
        with CaptureQueriesContext(connection) as ctx:
            ancestors = leaf.ancestor_chain()
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual([wiki.pk for wiki in ancestors], [wiki.pk for wiki in reversed(chain[:-1])])
        self.assertTrue(chain[0].would_create_cycle(leaf))
        self.assertFalse(leaf.would_create_cycle(chain[0]))
