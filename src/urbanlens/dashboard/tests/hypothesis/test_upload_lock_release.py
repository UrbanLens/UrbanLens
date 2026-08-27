"""A slow upload must not release the next upload's quota lock.

``per_profile_upload_lock`` serialises the read-usage-then-create-row sequence so
two near-simultaneous uploads can't both pass a quota check before either commits.
It released with a bare ``cache.delete`` guarded only by "did I acquire it",
which is not the same as "do I still hold it": an upload slower than the 30s
timeout has already lost the lock to the next one, and its release then drops
*that* upload's lock, letting a third in alongside it.

It now uses the shared token-checked release from ``services.core.locks``, so a
release only happens while the lock is still ours.

The lock is deliberately fail-open - a caller that cannot acquire it proceeds
anyway rather than blocking an upload - so these tests assert the release
behaviour, not mutual exclusion under contention.
"""

from __future__ import annotations

from django.core.cache import cache
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.storage import per_profile_upload_lock


class UploadLockReleaseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.key = f"upload-quota-lock:{self.profile.pk}"
        cache.delete(self.key)
        self.addCleanup(cache.delete, self.key)

    def test_it_reports_acquisition_and_releases_on_exit(self) -> None:
        with per_profile_upload_lock(self.profile) as acquired:
            self.assertTrue(acquired)

        with per_profile_upload_lock(self.profile) as acquired_again:
            self.assertTrue(acquired_again, "the lock was not released on exit")

    def test_a_second_holder_is_told_it_did_not_acquire(self) -> None:
        """Fail-open: the caller proceeds, but knows it has no safety net."""
        with per_profile_upload_lock(self.profile) as first:
            self.assertTrue(first)
            with per_profile_upload_lock(self.profile) as second:
                self.assertFalse(second)

    def test_an_overrunning_upload_does_not_release_the_next_ones_lock(self) -> None:
        """The bug: A's TTL lapses, B acquires, A's exit deletes B's lock."""
        from urbanlens.dashboard.services.core.locks import acquire_lock

        with per_profile_upload_lock(self.profile) as acquired:
            self.assertTrue(acquired)
            cache.delete(self.key)  # A's lock expires mid-upload
            successor = acquire_lock(self.key, 30)  # B starts
            self.assertIsNotNone(successor)

        self.assertIsNone(
            acquire_lock(self.key, 30),
            "the slow upload released a lock it no longer held, so a third upload could start",
        )

    def test_the_lock_is_released_when_the_body_raises(self) -> None:
        """Quota checks raise on rejection; that must not strand the lock."""
        with pytest.raises(RuntimeError), per_profile_upload_lock(self.profile):
            raise RuntimeError("upload rejected")

        with per_profile_upload_lock(self.profile) as acquired:
            self.assertTrue(acquired, "a failed upload stranded the lock")

    def test_locks_are_scoped_per_profile(self) -> None:
        other = Profile.objects.get(user=baker.make("auth.User"))
        self.addCleanup(cache.delete, f"upload-quota-lock:{other.pk}")

        with per_profile_upload_lock(self.profile) as mine, per_profile_upload_lock(other) as theirs:
            self.assertTrue(mine)
            self.assertTrue(theirs, "one profile's upload blocked another's")
