"""Equal-weight evidence clusters must not be ordered by comparing their *values*.

``_cluster_categorical`` builds ``(weight, value)`` tuples and sorts them
``reverse=True``. Tuple ordering falls through to the second element whenever the
first ties, so two clusters of identical weight get ranked by comparing their
values — which is never what was intended, and raises when the values are not
mutually comparable.

That is reachable rather than theoretical. ``FactEvidence`` stores ``data_type``
per row precisely so "old rows stay interpretable" after a fact key's registered
type changes (see the model docstring), so one fact can legitimately hold a
`value_text` row and a `value_bool` row at once. Every ``value_*`` column is
nullable too, so `None` can reach the same comparison. Either combination raises
``TypeError`` inside ``recompute_fact_confidence`` — a Celery task, so the fact
silently stops being recomputed.

Sorting by weight alone fixes it. Python's sort is stable, so ties then keep
evidence order instead of value order; both are arbitrary, but only one of them
can crash.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.facts.confidence import _cluster_categorical, _WeightedEvidence

_HYP = {"max_examples": 200, "deadline": None}

#: Every value shape a categorical fact can actually hold. `data_type` lives on
#: each evidence row so one fact can mix them after a key's registered type
#: changes, and every `value_*` column is nullable - so the clusterer has to
#: cope with any combination of these, including across a weight tie.
_FACT_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=20),
    st.integers(min_value=-1000, max_value=1000),
    st.dates(),
)
#: Deliberately coarse so exact ties are common - ties are the interesting case.
_TIE_PRONE_WEIGHTS = st.sampled_from([0.5, 1.0, 1.5, 2.0])


class ClusterTieBreakTests(SimpleTestCase):
    """Ranking clusters must depend on weight only."""

    def test_equal_weight_clusters_with_mixed_types_do_not_raise(self) -> None:
        """A text row and a bool row for the same fact - possible after a data_type change."""
        weighted = [
            _WeightedEvidence(value="brick", weight=1.5),
            _WeightedEvidence(value=True, weight=1.5),
        ]

        totals, total_weight = _cluster_categorical(weighted)

        self.assertEqual(total_weight, 3.0)
        self.assertEqual(sorted(w for w, _v in totals), [1.5, 1.5])

    def test_equal_weight_clusters_with_a_null_value_do_not_raise(self) -> None:
        """Every ``value_*`` column is nullable, so `None` can reach the comparison."""
        weighted = [
            _WeightedEvidence(value=None, weight=2.0),
            _WeightedEvidence(value="brick", weight=2.0),
        ]

        totals, total_weight = _cluster_categorical(weighted)

        self.assertEqual(total_weight, 4.0)
        self.assertEqual(len(totals), 2)

    def test_the_heavier_cluster_still_ranks_first(self) -> None:
        weighted = [
            _WeightedEvidence(value="light", weight=1.0),
            _WeightedEvidence(value="heavy", weight=9.0),
        ]

        totals, _total_weight = _cluster_categorical(weighted)

        self.assertEqual(totals[0][1], "heavy")

    def test_agreeing_values_still_merge_into_one_cluster(self) -> None:
        """Case-insensitive agreement is what `_values_agree` promises."""
        weighted = [
            _WeightedEvidence(value="Brick", weight=1.0),
            _WeightedEvidence(value="brick ", weight=2.0),
        ]

        totals, total_weight = _cluster_categorical(weighted)

        self.assertEqual(len(totals), 1)
        self.assertEqual(total_weight, 3.0)


class ClusterCategoricalPropertyTests(SimpleTestCase):
    """Invariants that must hold for *any* evidence list.

    The example-based tests above pin the two combinations that actually crashed.
    These generalise them: the existing suite only ever exercised
    ``_cluster_categorical`` with a single ``"a"``-valued row, which is why a
    tie-break over mixed types went unnoticed.
    """

    @given(values=st.lists(_FACT_VALUES, min_size=1, max_size=8), weight=_TIE_PRONE_WEIGHTS)
    @settings(**_HYP)
    def test_never_raises_whatever_the_values(self, values: list, weight: float) -> None:
        """Uniform weights make ties certain, so any value pair can meet in the sort."""
        _cluster_categorical([_WeightedEvidence(value=v, weight=weight) for v in values])

    @given(pairs=st.lists(st.tuples(_FACT_VALUES, _TIE_PRONE_WEIGHTS), min_size=1, max_size=8))
    @settings(**_HYP)
    def test_cluster_weights_sum_to_the_reported_total(self, pairs: list) -> None:
        weighted = [_WeightedEvidence(value=v, weight=w) for v, w in pairs]

        totals, total_weight = _cluster_categorical(weighted)

        self.assertAlmostEqual(sum(w for w, _v in totals), total_weight, places=6)
        self.assertAlmostEqual(total_weight, sum(w for _v, w in pairs), places=6)

    @given(pairs=st.lists(st.tuples(_FACT_VALUES, _TIE_PRONE_WEIGHTS), min_size=1, max_size=8))
    @settings(**_HYP)
    def test_clusters_are_ordered_heaviest_first(self, pairs: list) -> None:
        totals, _total = _cluster_categorical([_WeightedEvidence(value=v, weight=w) for v, w in pairs])

        weights = [w for w, _v in totals]
        self.assertEqual(weights, sorted(weights, reverse=True))

    @given(pairs=st.lists(st.tuples(_FACT_VALUES, _TIE_PRONE_WEIGHTS), min_size=1, max_size=8))
    @settings(**_HYP)
    def test_clustering_never_invents_or_drops_evidence(self, pairs: list) -> None:
        """Agreement can merge rows, so cluster count is between 1 and the input size."""
        totals, _total = _cluster_categorical([_WeightedEvidence(value=v, weight=w) for v, w in pairs])

        self.assertGreaterEqual(len(totals), 1)
        self.assertLessEqual(len(totals), len(pairs))
