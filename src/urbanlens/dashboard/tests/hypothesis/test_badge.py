"""Property-based tests for Badge hierarchy.

Badge.get_badge_and_descendants performs a BFS over the self-referential
parents M2M.  Key invariants:

1. The seed badge ID is always in the result.
2. A leaf badge (no children) returns a singleton set.
3. All direct children of the seed appear in the result.
4. The algorithm terminates even when cycles are present.
5. The result is monotonically non-decreasing as more descendants are added.
"""
from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.badges.model import Badge, KIND_TAG

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _make_tag(name: str = "tag", **kwargs) -> Badge:
    return baker.make(Badge, name=name, kind=KIND_TAG, profile=None, **kwargs)


class BadgeDescendantSeedTests(TestCase):
    """get_badge_and_descendants always includes the seed."""

    def test_seed_id_always_in_result(self) -> None:
        tag = _make_tag()
        result = Badge.get_badge_and_descendants(tag.pk)
        self.assertIn(tag.pk, result)

    def test_leaf_badge_returns_singleton(self) -> None:
        """A badge with no children should return exactly {badge_id}."""
        tag = _make_tag()
        result = Badge.get_badge_and_descendants(tag.pk)
        self.assertEqual(result, {tag.pk})

    @given(n_children=st.integers(min_value=1, max_value=6))
    @_db_settings
    def test_all_direct_children_included(self, n_children: int) -> None:
        parent = _make_tag(name="parent")
        children = [_make_tag(name=f"child_{i}") for i in range(n_children)]
        for child in children:
            child.parents.add(parent)
        result = Badge.get_badge_and_descendants(parent.pk)
        child_ids = {c.pk for c in children}
        self.assertTrue(child_ids.issubset(result), f"Missing children: {child_ids - result}")

    @given(n_children=st.integers(min_value=1, max_value=4))
    @_db_settings
    def test_parent_is_always_in_result(self, n_children: int) -> None:
        parent = _make_tag(name="parent")
        for i in range(n_children):
            child = _make_tag(name=f"child_{i}")
            child.parents.add(parent)
        result = Badge.get_badge_and_descendants(parent.pk)
        self.assertIn(parent.pk, result)

    @given(depth=st.integers(min_value=2, max_value=5))
    @_db_settings
    def test_multi_level_chain_all_included(self, depth: int) -> None:
        """A linear chain root → child → grandchild → … must all appear."""
        nodes = [_make_tag(name=f"node_{i}") for i in range(depth)]
        # link as a chain: nodes[i+1].parents.add(nodes[i])
        for i in range(depth - 1):
            nodes[i + 1].parents.add(nodes[i])
        result = Badge.get_badge_and_descendants(nodes[0].pk)
        self.assertEqual(result, {n.pk for n in nodes})


class BadgeCycleTests(TestCase):
    """The BFS must terminate even when cycles are present in the parents M2M."""

    def test_direct_self_reference_terminates(self) -> None:
        tag = _make_tag()
        # Create an artificial cycle: tag is its own parent.
        tag.parents.add(tag)
        # This must not loop forever.
        result = Badge.get_badge_and_descendants(tag.pk)
        self.assertIn(tag.pk, result)

    def test_two_node_cycle_terminates(self) -> None:
        tag_a = _make_tag(name="a")
        tag_b = _make_tag(name="b")
        tag_b.parents.add(tag_a)
        tag_a.parents.add(tag_b)  # cycle
        result_a = Badge.get_badge_and_descendants(tag_a.pk)
        result_b = Badge.get_badge_and_descendants(tag_b.pk)
        # Both nodes must be reachable from either start.
        self.assertIn(tag_a.pk, result_a)
        self.assertIn(tag_b.pk, result_a)
        self.assertIn(tag_a.pk, result_b)
        self.assertIn(tag_b.pk, result_b)

    def test_three_node_cycle_terminates(self) -> None:
        nodes = [_make_tag(name=f"cycle_{i}") for i in range(3)]
        for i in range(3):
            nodes[(i + 1) % 3].parents.add(nodes[i])
        for node in nodes:
            result = Badge.get_badge_and_descendants(node.pk)
            self.assertEqual(result, {n.pk for n in nodes})


class BadgeDescendantMonotonicityTests(TestCase):
    """Adding more children must never shrink the descendant set."""

    @given(n_extra=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_adding_child_never_reduces_result_size(self, n_extra: int) -> None:
        parent = _make_tag(name="parent")
        initial = Badge.get_badge_and_descendants(parent.pk)
        for i in range(n_extra):
            child = _make_tag(name=f"added_{i}")
            child.parents.add(parent)
        after = Badge.get_badge_and_descendants(parent.pk)
        self.assertGreaterEqual(len(after), len(initial))
        self.assertTrue(initial.issubset(after))

    @given(n_children=st.integers(min_value=1, max_value=5))
    @_db_settings
    def test_result_is_exactly_seed_plus_descendants(self, n_children: int) -> None:
        """Every ID in the result must be either the seed or a genuine descendant."""
        parent = _make_tag(name="root")
        children = [_make_tag(name=f"ch_{i}") for i in range(n_children)]
        for ch in children:
            ch.parents.add(parent)
        result = Badge.get_badge_and_descendants(parent.pk)
        allowed_ids = {parent.pk} | {ch.pk for ch in children}
        # Ensure no unexpected IDs leaked in from other tests (thanks to transaction isolation).
        # Every member of result must be in allowed_ids.
        extra = result - allowed_ids
        self.assertFalse(extra, f"Unexpected IDs in result: {extra}")


class BadgeResultTypeTests(TestCase):
    """get_badge_and_descendants must always return a set of ints."""

    def test_return_type_is_set(self) -> None:
        tag = _make_tag()
        result = Badge.get_badge_and_descendants(tag.pk)
        self.assertIsInstance(result, set)

    def test_all_elements_are_ints(self) -> None:
        parent = _make_tag()
        child = _make_tag()
        child.parents.add(parent)
        result = Badge.get_badge_and_descendants(parent.pk)
        for item in result:
            self.assertIsInstance(item, int)
