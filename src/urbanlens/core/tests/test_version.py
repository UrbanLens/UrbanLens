"""Tests for application version and git deployment metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
import importlib.util
import json
from pathlib import Path
import sys
import tomllib
import unittest
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.core.version import (
    PYPROJECT_PATH,
    _git_fetch,
    format_short_commit,
    get_app_version,
    get_current_git_branch,
    get_current_git_commit,
    get_git_update_status,
    get_upstream_git_commit,
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
        with PYPROJECT_PATH.open("rb") as pyproject_file:
            expected = tomllib.load(pyproject_file)["project"]["version"]
        with mock.patch("urbanlens.core.version.pkg_version", return_value="0.0.0-installed"):
            self.assertEqual(get_app_version(), expected)

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


class PreOneReleaseTests(SimpleTestCase):
    """
    UrbanLens stays on 0.x releases until the maintainer explicitly decides otherwise.

    A 1.0.0 commits the project to semver's stability promise, and UrbanLens is still in
    beta. A release tool following semver would turn one breaking-change commit into a 1.0.0
    release PR, so bin/release.py must keep a breaking change on the minor version
    (release-please's bump-minor-pre-major). Changing these assertions is that explicit
    decision, not a test fix.
    """

    repo_root = PYPROJECT_PATH.parent

    @unittest.skipUnless((PYPROJECT_PATH.parent / "bin" / "release.py").exists(), "this checkout has no release tool")
    def test_the_release_tool_keeps_breaking_changes_below_1_0(self) -> None:
        spec = importlib.util.spec_from_file_location("release", self.repo_root / "bin" / "release.py")
        assert spec is not None and spec.loader is not None
        release = sys.modules.get("release") or importlib.util.module_from_spec(spec)
        if "release" not in sys.modules:
            sys.modules["release"] = release
            spec.loader.exec_module(release)
        self.assertEqual(release.next_version("0.9.4", minor=True), "0.10.0")
        with self.assertRaises(release.ReleaseError):
            release.next_version("1.0.0", minor=False)
        cliff = tomllib.loads((self.repo_root / "cliff.toml").read_text())
        self.assertIs(cliff["bump"]["breaking_always_bump_major"], False)

    def test_every_recorded_version_is_0_x(self) -> None:
        with PYPROJECT_PATH.open("rb") as pyproject_file:
            pyproject_version = tomllib.load(pyproject_file)["project"]["version"]
        manifest = json.loads((self.repo_root / ".release-please-manifest.json").read_text())
        package_json = json.loads((self.repo_root / "package.json").read_text())
        recorded = {
            "pyproject.toml": pyproject_version,
            "package.json": package_json["version"],
            **{f".release-please-manifest.json[{path}]": version for path, version in manifest.items()},
        }
        for source, version in recorded.items():
            with self.subTest(source=source):
                self.assertTrue(str(version).startswith("0."), msg=f"{source} records {version}")
