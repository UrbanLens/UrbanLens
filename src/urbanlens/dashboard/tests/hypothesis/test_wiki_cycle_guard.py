"""Property-based tests for Wiki.would_create_cycle.

Wiki.parent_wiki allows wikis to be nested to arbitrary depth (a child wiki
may itself have child wikis), mirroring Pin.parent_pin. The only thing that
must never happen is a loop in that chain (A -> B -> C -> A), since future
code walks the chain assuming it terminates. Key invariants:

1. A None parent never creates a cycle.
2. A wiki can never become its own parent.
3. Making a wiki the parent of its own ancestor closes a loop and is rejected.
4. This holds at arbitrary chain depth, not just directly adjacent wikis.
5. Unrelated wikis never register as a cycle.
6. The check terminates even against data that already contains a corrupted cycle.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.model import Wiki

_db_settings = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _make_wiki(**kwargs) -> Wiki:
    kwargs.setdefault("location", baker.make("dashboard.Location"))
    return baker.make(Wiki, **kwargs)


class WikiCycleGuardBasicTests(TestCase):
    """Straightforward, non-hypothesis cases."""

    def test_none_parent_never_creates_cycle(self) -> None:
        wiki = _make_wiki()
        self.assertFalse(wiki.would_create_cycle(None))

    def test_wiki_cannot_be_its_own_parent(self) -> None:
        wiki = _make_wiki()
        self.assertTrue(wiki.would_create_cycle(wiki))

    def test_unrelated_wikis_are_not_a_cycle(self) -> None:
        a = _make_wiki()
        b = _make_wiki()
        self.assertFalse(a.would_create_cycle(b))

    def test_making_existing_child_the_new_parent_is_a_cycle(self) -> None:
        """A -> B already exists; A.would_create_cycle(B) must be True (would close A -> B -> A)."""
        a = _make_wiki()
        b = _make_wiki(parent_wiki=a)
        self.assertTrue(a.would_create_cycle(b))

    def test_grandchild_as_new_parent_is_a_cycle(self) -> None:
        """A -> B -> C already exists; A.would_create_cycle(C) must be True."""
        a = _make_wiki()
        b = _make_wiki(parent_wiki=a)
        c = _make_wiki(parent_wiki=b)
        self.assertTrue(a.would_create_cycle(c))

    def test_unsaved_wiki_can_never_be_an_ancestor(self) -> None:
        """A brand-new (unsaved) wiki has no pk, so it can't already appear in any chain."""
        existing_parent = _make_wiki()
        unsaved = Wiki(name="Unsaved", location=baker.make("dashboard.Location"))
        self.assertFalse(unsaved.would_create_cycle(existing_parent))

    def test_terminates_against_a_preexisting_corrupted_cycle(self) -> None:
        """A pre-existing A<->B loop (bypassing the guard) must not hang an unrelated check."""
        a = _make_wiki()
        b = _make_wiki(parent_wiki=a)
        # Bypass the guard directly to simulate already-corrupted data.
        a.parent_wiki = b
        a.save(update_fields=["parent_wiki"])

        unrelated = _make_wiki()
        self.assertFalse(unrelated.would_create_cycle(a))


class WikiCycleGuardDepthTests(TestCase):
    """The guard must hold at arbitrary chain depth, not just directly adjacent wikis."""

    @given(depth=st.integers(min_value=1, max_value=8))
    @_db_settings
    def test_closing_the_loop_at_the_far_end_of_a_chain_is_detected(self, depth: int) -> None:
        """Build a depth-long chain root -> ... -> tail, then check tail.would_create_cycle(root)."""
        root = _make_wiki()
        chain = [root]
        for _ in range(depth):
            chain.append(_make_wiki(parent_wiki=chain[-1]))
        tail = chain[-1]
        # Making root's parent the tail would close the loop, unless tail is root itself.
        self.assertTrue(root.would_create_cycle(tail))

    @given(depth=st.integers(min_value=1, max_value=8))
    @_db_settings
    def test_chain_members_are_not_falsely_flagged_against_unrelated_wiki(self, depth: int) -> None:
        root = _make_wiki()
        chain = [root]
        for _ in range(depth):
            chain.append(_make_wiki(parent_wiki=chain[-1]))
        unrelated = _make_wiki()
        self.assertFalse(unrelated.would_create_cycle(chain[-1]))
