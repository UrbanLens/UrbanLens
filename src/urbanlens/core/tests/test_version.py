"""Tests for application version and git deployment metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
import subprocess
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.core.version import (
    _git_fetch,
    apply_pending_migrations,
    format_short_commit,
    get_app_version,
    get_current_git_branch,
    get_current_git_commit,
    get_git_update_status,
    get_upstream_git_commit,
    pull_latest_git_code,
    trigger_development_app_reload,
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
        self.assertEqual(version, "0.8.0b0")

    def test_falls_back_to_installed_package_when_pyproject_unreadable(self) -> None:
        missing_path = Path("nonexistent") / "pyproject.toml"
        with (
            mock.patch("urbanlens.core.version.PYPROJECT_PATH", missing_path),
            mock.patch("urbanlens.core.version.pkg_version", return_value="9.9.9"),
        ):
            self.assertEqual(get_app_version(), "9.9.9")

    def test_returns_zero_version_when_no_source_available(self) -> None:
        missing_path = Path("nonexistent") / "pyproject.toml"
        with (
            mock.patch("urbanlens.core.version.PYPROJECT_PATH", missing_path),
            mock.patch("urbanlens.core.version.pkg_version", side_effect=PackageNotFoundError),
        ):
            self.assertEqual(get_app_version(), "0.0.0")


class FormatShortCommitTests(TestCase):
    """format_short_commit shortens hashes for display."""

    def test_shortens_full_hash(self) -> None:
        commit = "abcdef1234567890"
        self.assertEqual(format_short_commit(commit), "abcdef1")

    def test_missing_commit_returns_em_dash(self) -> None:
        self.assertEqual(format_short_commit(None), "-")


class GetCurrentGitBranchTests(TestCase):
    """get_current_git_branch reads the checked-out branch name."""

    def test_returns_branch_name(self) -> None:
        completed = mock.Mock(returncode=0, stdout="main\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed):
            self.assertEqual(get_current_git_branch(), "main")

    def test_git_failure_returns_none(self) -> None:
        with mock.patch("urbanlens.core.version.subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(get_current_git_branch())

    def test_detached_head_returns_none(self) -> None:
        completed = mock.Mock(returncode=0, stdout="\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed):
            self.assertIsNone(get_current_git_branch())


class GitFetchTests(TestCase):
    """_git_fetch handles unreachable remotes without tracebacks."""

    def setUp(self) -> None:
        _git_fetch.cache_clear()

    def test_no_remotes_skips_fetch(self) -> None:
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed) as run:
            self.assertFalse(_git_fetch())
        run.assert_called_once()

    def test_fetch_failure_returns_false(self) -> None:
        remote_ok = mock.Mock(returncode=0, stdout="origin\n", stderr="")
        fetch_failed = mock.Mock(returncode=128, stdout="", stderr="fatal: could not read from remote")
        with mock.patch(
            "urbanlens.core.version.subprocess.run",
            side_effect=[remote_ok, fetch_failed],
        ):
            self.assertFalse(_git_fetch())

    def test_successful_fetch_returns_true(self) -> None:
        remote_ok = mock.Mock(returncode=0, stdout="origin\n", stderr="")
        fetch_ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch(
            "urbanlens.core.version.subprocess.run",
            side_effect=[remote_ok, fetch_ok],
        ):
            self.assertTrue(_git_fetch())


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

    def test_missing_deployed_commit_reports_up_to_date(self) -> None:
        current = "fed987cba654"
        with (
            mock.patch("urbanlens.core.version._git_fetch", return_value=True),
            mock.patch("urbanlens.core.version.get_current_git_commit", return_value=current),
            mock.patch("urbanlens.core.version.get_upstream_git_commit", return_value=current),
            mock.patch("urbanlens.core.version._count_commits_ahead") as count_ahead,
        ):
            status = get_git_update_status(None)
        count_ahead.assert_not_called()
        self.assertTrue(status.git_available)
        self.assertIsNone(status.deployed_commit)
        self.assertEqual(status.current_commit, current)
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


class TriggerDevelopmentAppReloadTests(TestCase):
    """trigger_development_app_reload signals gunicorn or runserver appropriately."""

    def test_gunicorn_parent_receives_sighup(self) -> None:
        with (
            mock.patch("urbanlens.core.version._parent_process_command", return_value="gunicorn: master"),
            mock.patch("urbanlens.core.version.os.kill") as kill,
        ):
            ok, message = trigger_development_app_reload()

        self.assertTrue(ok)
        self.assertIn("reload", message.lower())
        kill.assert_called_once()

    def test_runserver_fallback_touches_imported_module(self) -> None:
        with (
            mock.patch("urbanlens.core.version._parent_process_command", return_value="python manage.py runserver"),
            mock.patch("urbanlens.core.version.os.utime") as utime,
        ):
            ok, message = trigger_development_app_reload()

        self.assertTrue(ok)
        self.assertIn("reload", message.lower())
        utime.assert_called_once()

    def test_reload_failure_returns_false(self) -> None:
        with (
            mock.patch("urbanlens.core.version._parent_process_command", return_value="gunicorn: master"),
            mock.patch("urbanlens.core.version.os.kill", side_effect=OSError("permission denied")),
        ):
            ok, message = trigger_development_app_reload()

        self.assertFalse(ok)
        self.assertIn("reload", message.lower())

    def test_runserver_fallback_failure_returns_false(self) -> None:
        with (
            mock.patch("urbanlens.core.version._parent_process_command", return_value="python manage.py runserver"),
            mock.patch("urbanlens.core.version.os.utime", side_effect=OSError("read-only filesystem")),
        ):
            ok, message = trigger_development_app_reload()

        self.assertFalse(ok)
        self.assertIn("reload", message.lower())


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

    def test_timeout_returns_safe_message(self) -> None:
        with mock.patch(
            "urbanlens.core.version.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git pull --ff-only", timeout=120),
        ):
            ok, message = pull_latest_git_code()

        self.assertFalse(ok)
        self.assertIn("Timed out", message)

    def test_cannot_start_git_returns_safe_message(self) -> None:
        with mock.patch("urbanlens.core.version.subprocess.run", side_effect=OSError("git not found")):
            ok, message = pull_latest_git_code()

        self.assertFalse(ok)
        self.assertIn("Could not start", message)


class ApplyPendingMigrationsTests(SimpleTestCase):
    """apply_pending_migrations runs Django migrate after a code update."""

    def test_success_returns_message(self) -> None:
        completed = mock.Mock(returncode=0, stdout="Applying dashboard.0042... OK\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed) as run:
            ok, message = apply_pending_migrations()

        self.assertTrue(ok)
        self.assertIn("Applying dashboard.0042", message)
        self.assertIn("migrate", run.call_args.args[0])
        self.assertIn("--noinput", run.call_args.args[0])

    def test_failure_returns_output(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="django.db.utils.OperationalError")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed):
            ok, message = apply_pending_migrations()

        self.assertFalse(ok)
        self.assertIn("OperationalError", message)

    def test_timeout_returns_safe_message(self) -> None:
        with mock.patch(
            "urbanlens.core.version.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="manage.py migrate", timeout=300),
        ):
            ok, message = apply_pending_migrations()

        self.assertFalse(ok)
        self.assertIn("Timed out", message)

    def test_cannot_start_returns_safe_message(self) -> None:
        with mock.patch("urbanlens.core.version.subprocess.run", side_effect=OSError("python not found")):
            ok, message = apply_pending_migrations()

        self.assertFalse(ok)
        self.assertIn("Could not start", message)


class GitRevisionHelperTests(SimpleTestCase):
    """get_current_git_commit/get_upstream_git_commit resolve distinct revisions."""

    def test_current_commit_resolves_head(self) -> None:
        completed = mock.Mock(returncode=0, stdout="deadbeef1234\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed) as run:
            self.assertEqual(get_current_git_commit(), "deadbeef1234")
        self.assertIn("HEAD", run.call_args.args[0])

    def test_upstream_commit_resolves_upstream_tracking_ref(self) -> None:
        completed = mock.Mock(returncode=0, stdout="cafebabe5678\n", stderr="")
        with mock.patch("urbanlens.core.version.subprocess.run", return_value=completed) as run:
            self.assertEqual(get_upstream_git_commit(), "cafebabe5678")
        self.assertIn("@{u}", run.call_args.args[0])

    def test_upstream_commit_missing_returns_none(self) -> None:
        with mock.patch("urbanlens.core.version.subprocess.run", side_effect=OSError("no upstream")):
            self.assertIsNone(get_upstream_git_commit())
