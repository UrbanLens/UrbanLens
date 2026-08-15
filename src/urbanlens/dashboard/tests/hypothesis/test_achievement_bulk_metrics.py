"""The bulk metric variants agree with the per-profile ones, and the sweep uses them.

The nightly sweep cost ~30 queries per profile (measured in PROBLEMS.md's
entry) because every metric was an independent per-profile count. Each metric
now carries a ``compute_bulk`` returning ``{profile_id: value}`` from one
grouped query. The invariant that matters is **agreement**: for any data
state, the bulk dict must give every profile exactly the value the
per-profile ``compute`` would - absent meaning 0. A bulk variant that drifts
would grant or withhold awards differently on the nightly path than on the
per-write signal path, which users would experience as awards flapping.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.achievements import metrics as metrics_module
from urbanlens.dashboard.services.achievements.evaluate import evaluate_profile
from urbanlens.dashboard.services.achievements.metrics import all_metrics, compute_values_bulk


class BulkPerProfileAgreementTests(TestCase):
    """Every metric with a bulk variant agrees with its per-profile compute."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # bootstrap admin
        self.profiles = [baker.make(User).profile for _ in range(3)]
        first, second, _third = self.profiles

        # Content spread unevenly so agreement is non-vacuous: profile 1 has
        # pins/reviews/ratings, profile 2 a wiki edit and comments, profile 3
        # nothing at all (the absent-means-zero case).
        location = baker.make("dashboard.Location", latitude=40.1, longitude=-74.1)
        baker.make_recipe("dashboard.pin", profile=first, location=location, vulnerability=3)
        baker.make_recipe("dashboard.pin", profile=first, location=baker.make("dashboard.Location", latitude=40.2, longitude=-74.2), danger=2)
        baker.make("dashboard.Review", profile=first, pin=first.pins.first(), rating=4)
        baker.make("dashboard.Comment", profile=second)
        baker.make("dashboard.Comment", profile=second)

    def test_every_bulk_metric_agrees_with_per_profile_compute(self) -> None:
        checked = 0
        for metric in all_metrics():
            bulk = metric.bulk_values()
            if bulk is None:
                continue
            checked += 1
            for profile in self.profiles:
                self.assertEqual(
                    bulk.get(profile.pk, 0),
                    metric.value_for(profile),
                    f"metric {metric.key}: bulk and per-profile disagree for profile {profile.pk}",
                )
        # 14 direct + 5 streak metrics carry bulk variants; a refactor that
        # silently unwires them would make this suite pass vacuously.
        self.assertGreaterEqual(checked, 19, "expected every builtin metric to carry a bulk variant")

    def test_bulk_omits_zero_profiles_rather_than_storing_them(self) -> None:
        """Absent-means-zero is the memory contract - a 100k-profile deployment must not hold 100k zeros per metric."""
        pins_bulk = next(m for m in all_metrics() if m.key == "pins_created").bulk_values()
        assert pins_bulk is not None
        zero_profile = self.profiles[2]
        self.assertNotIn(zero_profile.pk, pins_bulk)


class EvaluateProfileWithPrecomputedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.achievement = baker.make("dashboard.Achievement", metric="pins_created", threshold=1, is_active=True)

    def test_precomputed_values_are_used_without_per_profile_queries(self) -> None:
        precomputed = compute_values_bulk({"pins_created"})
        with mock.patch.object(metrics_module, "compute_values", side_effect=AssertionError("per-profile path must not run")) as _guard:
            with mock.patch("urbanlens.dashboard.services.achievements.evaluate.compute_values", side_effect=AssertionError("per-profile path must not run")):
                granted = evaluate_profile(self.profile, notify=False, precomputed=precomputed)
        self.assertEqual(granted, [], "no pins exist, so nothing should be granted")

    def test_precomputed_grants_from_the_bulk_value(self) -> None:
        location = baker.make("dashboard.Location", latitude=41.0, longitude=-75.0)
        baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        precomputed = compute_values_bulk({"pins_created"})
        granted = evaluate_profile(self.profile, notify=False, precomputed=precomputed)
        self.assertEqual(len(granted), 1)
        self.assertEqual(granted[0].achievement, self.achievement)
