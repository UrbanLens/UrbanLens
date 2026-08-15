"""The nightly achievement sweep must not always abandon the same profiles.

`evaluate_all_profiles` costs ~30 queries per profile and runs as a single task
under a hard 3600s Celery limit, so past a certain user count a run is killed
part-way through. With a plain `Profile.objects.iterator()` the ordering is
stable, so the *same* tail of profiles was truncated every night - permanently,
and silently, because the task just dies. Those users stop earning the awards
that only this safety net catches (thresholds crossed by time passing rather
than by a write).

Progress is now checkpointed, so a killed run resumes rather than restarting,
and reaching the end resets the cursor so the next run covers whatever the
resumed one skipped. This does not make the sweep cheaper - see
`docs/PROBLEMS.md` for the batching fix - it stops the cost from falling on one
fixed group of users.
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.achievements import evaluate as evaluate_module
from urbanlens.dashboard.services.achievements.evaluate import _SWEEP_CURSOR_CACHE_KEY, evaluate_all_profiles


class SweepResumeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.delete(_SWEEP_CURSOR_CACHE_KEY)
        self.addCleanup(cache.delete, _SWEEP_CURSOR_CACHE_KEY)
        baker.make("dashboard.Achievement", is_active=True)
        self.profiles = sorted(Profile.objects.get(user=baker.make("auth.User")).pk for _ in range(6))

    def _seen_profile_ids(self) -> list[int]:
        """Run the sweep, recording which profiles it actually evaluated."""
        seen: list[int] = []

        def _record(profile, notify=True, **kwargs):
            seen.append(profile.pk)
            return []

        with patch.object(evaluate_module, "evaluate_profile", side_effect=_record):
            evaluate_all_profiles()
        return seen

    def test_a_complete_run_visits_every_profile_and_resets_the_cursor(self) -> None:
        seen = self._seen_profile_ids()

        self.assertEqual(sorted(seen), self.profiles)
        self.assertEqual(cache.get(_SWEEP_CURSOR_CACHE_KEY), 0)

    def test_a_run_resumes_after_the_recorded_cursor(self) -> None:
        """The behaviour a killed run depends on: pick up, don't restart."""
        cache.set(_SWEEP_CURSOR_CACHE_KEY, self.profiles[2], timeout=None)

        seen = self._seen_profile_ids()

        self.assertEqual(sorted(seen), self.profiles[3:], "a resumed run must skip what the killed run already did")

    def test_the_profiles_a_resumed_run_skipped_are_covered_by_the_next_run(self) -> None:
        """Two runs reach everyone even when the first started mid-way.

        This is the property that matters: no profile can be starved forever.
        """
        cache.set(_SWEEP_CURSOR_CACHE_KEY, self.profiles[2], timeout=None)

        first = self._seen_profile_ids()
        second = self._seen_profile_ids()

        self.assertEqual(sorted(set(first) | set(second)), self.profiles)

    def test_resuming_is_reported(self) -> None:
        """The truncation was invisible before; a resumed run has to say so."""
        cache.set(_SWEEP_CURSOR_CACHE_KEY, self.profiles[1], timeout=None)

        with self.assertLogs("urbanlens.dashboard.services.achievements.evaluate", level="WARNING") as logs:
            self._seen_profile_ids()

        self.assertTrue(any("resuming" in line.lower() for line in logs.output))

    def test_a_clean_run_logs_no_warning(self) -> None:
        with patch.object(evaluate_module.logger, "warning") as mock_warning:
            self._seen_profile_ids()

        mock_warning.assert_not_called()
