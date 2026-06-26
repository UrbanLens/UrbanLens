"""Tests for application version and git deployment metadata."""
from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.core.version import (
    format_short_commit,
    get_app_version,
    get_git_update_status,
    pull_latest_git_code,
)


class AppVersionTests(TestCase):
    """get_app_version reads from the installed package or pyproject.toml."""

    def test_returns_semver_string(self) -> None:
        version = get_app_version()
        parts = version.split(".")
        self.assertGreaterEqual(len(parts), 2)
        for part in parts[:2]:
            self.assertTrue(part.isdigit(), msg=f"Expected numeric semver segment, got {part!r}")

    def test_pyproject_is_preferred_source(self) -> None:
        version = get_app_version()
        self.assertEqual(version, "0.2.0")


class FormatShortCommitTests(TestCase):
    """format_short_commit shortens hashes for display."""

    def test_shortens_full_hash(self) -> None:
        commit = "abcdef1234567890"
        self.assertEqual(format_short_commit(commit), "abcdef1")

    def test_missing_commit_returns_em_dash(self) -> None:
        self.assertEqual(format_short_commit(None), "—")


class GitUpdateStatusTests(TestCase):
    """get_git_update_status compares deployed and current commits."""

    def test_no_git_repo_reports_unavailable(self) -> None:
        with (
            mock.patch("urbanlens.core.version._git_fetch", return_value=False),
            mock.patch("urbanlens.core.version.get_current_git_commit", return_value=None),
            mock.patch("urbanlens.core.version.get_upstream_git_commit", return_value=None),
        ):
            status = get_git_update_status("abc123")
        self.assertFalse(status.git_available)
        self.assertFalse(status.has_newer_commits)

    def test_matching_commits_are_up_to_date(self) -> None:
        commit = "abc123def456"
        with (
            mock.patch("urbanlens.core.version._git_fetch", return_value=True),
            mock.patch("urbanlens.core.version.get_current_git_commit", return_value=commit),
            mock.patch("urbanlens.core.version.get_upstream_git_commit", return_value=commit),
            mock.patch("urbanlens.core.version._count_commits_ahead", return_value=0),
        ):
            status = get_git_update_status(commit)
        self.assertTrue(status.git_available)
        self.assertTrue(status.remote_refreshed)
        self.assertFalse(status.has_newer_commits)
        self.assertEqual(status.commits_ahead, 0)

    def test_ahead_commits_flag_update_available(self) -> None:
        deployed = "abc123def456"
        current = "fed987cba654"
        with (
            mock.patch("urbanlens.core.version._git_fetch", return_value=True),
            mock.patch("urbanlens.core.version.get_current_git_commit", return_value=current),
            mock.patch("urbanlens.core.version.get_upstream_git_commit", return_value=current),
            mock.patch("urbanlens.core.version._count_commits_ahead", return_value=3),
        ):
            status = get_git_update_status(deployed)
        self.assertTrue(status.has_newer_commits)
        self.assertEqual(status.commits_ahead, 3)

    def test_upstream_ahead_of_local_flags_update_available(self) -> None:
        deployed = "abc123def456"
        current = "abc123def456"
        upstream = "fed987cba654"
        with (
            mock.patch("urbanlens.core.version._git_fetch", return_value=True),
            mock.patch("urbanlens.core.version.get_current_git_commit", return_value=current),
            mock.patch("urbanlens.core.version.get_upstream_git_commit", return_value=upstream),
            mock.patch(
                "urbanlens.core.version._count_commits_ahead",
                side_effect=lambda _base, head: 0 if head == current else 2,
            ),
        ):
            status = get_git_update_status(deployed)
        self.assertTrue(status.has_newer_commits)
        self.assertEqual(status.commits_ahead, 2)
        self.assertEqual(status.upstream_commit, upstream)


class PullLatestGitCodeTests(TestCase):
    """pull_latest_git_code wraps git pull safely for the admin endpoint."""

    def test_success_returns_message(self) -> None:
        completed = mock.Mock(returncode=0, stdout="Already up to date.\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed) as run:
            ok, message = pull_latest_git_code()

        self.assertTrue(ok)
        self.assertEqual(message, "Already up to date.")
        run.assert_called_once()
        self.assertIn("GIT_TERMINAL_PROMPT", run.call_args.kwargs["env"])

    def test_failure_returns_safe_message(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="fatal: Not possible to fast-forward")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed):
            ok, message = pull_latest_git_code()

        self.assertFalse(ok)
        self.assertIn("fast-forward", message)
