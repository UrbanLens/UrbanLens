"""A sweep that outran its TTL must not release the next run's lock."""

from __future__ import annotations

from django.core.cache import cache

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.locks import acquire_lock, beat_lock, release_lock

_KEY = "urbanlens:test:sweep-lock"


class SweepLockReleaseTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.delete(_KEY)
        self.addCleanup(cache.delete, _KEY)

    def test_a_second_caller_is_refused_while_the_lock_is_held(self) -> None:
        self.assertIsNotNone(acquire_lock(_KEY, 60))

        self.assertIsNone(acquire_lock(_KEY, 60), "two runs acquired the same lock")

    def test_releasing_frees_it_for_the_next_run(self) -> None:
        token = acquire_lock(_KEY, 60)

        release_lock(_KEY, token)

        self.assertIsNotNone(acquire_lock(_KEY, 60))

    def test_an_overrun_run_does_not_release_the_new_holders_lock(self) -> None:
        """The bug: A overruns, B takes the lock, A's finally deletes B's."""
        slow_token = acquire_lock(_KEY, 60)
        cache.delete(_KEY)  # A's TTL lapses mid-run
        fast_token = acquire_lock(_KEY, 60)  # the next tick starts B
        self.assertIsNotNone(fast_token)

        release_lock(_KEY, slow_token)  # A finishes and releases

        self.assertIsNone(
            acquire_lock(_KEY, 60),
            "the overrunning run released a lock it no longer held, letting a third run start",
        )

    def test_the_overrun_is_logged_rather_than_passing_silently(self) -> None:
        """An overrun means a TTL is mistuned; it should be visible."""
        slow_token = acquire_lock(_KEY, 60)
        cache.delete(_KEY)
        acquire_lock(_KEY, 60)

        with self.assertLogs("urbanlens.dashboard.services.core.locks", level="WARNING") as logs:
            release_lock(_KEY, slow_token)

        self.assertTrue(any("outlived its TTL" in line for line in logs.output), logs.output)

    def test_releasing_without_having_acquired_is_a_no_op(self) -> None:
        """Callers that were refused release unconditionally in a finally."""
        holder = acquire_lock(_KEY, 60)
        refused = acquire_lock(_KEY, 60)
        self.assertIsNone(refused)

        release_lock(_KEY, refused)

        self.assertIsNone(acquire_lock(_KEY, 60), "the refused caller freed the holder's lock")
        release_lock(_KEY, holder)

    def test_the_context_manager_reports_and_releases(self) -> None:
        with beat_lock(_KEY, 60) as acquired:
            self.assertTrue(acquired)
            with beat_lock(_KEY, 60) as nested:
                self.assertFalse(nested, "a second holder got in")

        self.assertIsNotNone(acquire_lock(_KEY, 60), "the block did not release on exit")

    def test_the_context_manager_releases_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError), beat_lock(_KEY, 60) as acquired:
            self.assertTrue(acquired)
            raise RuntimeError("sweep blew up")

        self.assertIsNotNone(acquire_lock(_KEY, 60), "a crashed sweep left its lock behind")

    def test_an_expired_lock_with_no_new_holder_does_not_warn(self) -> None:
        """The overrun warning means "two runs overlapped" and is worth acting on.
        Raising it for a lock that simply expired with nobody waiting - or for a
        second release of the same token - buries the real signal in noise."""
        token = acquire_lock(_KEY, 30)
        cache.delete(_KEY)  # TTL expiry, nobody else took it

        with self.assertNoLogs("urbanlens.dashboard.services.core.locks", level="WARNING"):
            release_lock(_KEY, token)

    def test_a_double_release_does_not_warn(self) -> None:
        token = acquire_lock(_KEY, 30)
        release_lock(_KEY, token)

        with self.assertNoLogs("urbanlens.dashboard.services.core.locks", level="WARNING"):
            release_lock(_KEY, token)
