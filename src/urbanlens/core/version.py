"""Application version and deployment metadata."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
import logging
from pathlib import Path
import subprocess
import tomllib

from urbanlens.UrbanLens.settings.meta.app import DEFAULT_ROOT

logger = logging.getLogger(__name__)

PROJECT_NAME = "UrbanLens"
PYPROJECT_PATH = DEFAULT_ROOT.parent / "pyproject.toml"
_GIT_CWD = DEFAULT_ROOT.parent


@dataclass(frozen=True, slots=True)
class GitUpdateStatus:
    """Comparison between the deployed git commit and the current repository HEAD.

    Attributes:
        deployed_commit: Git commit hash recorded at deploy or process start.
        current_commit: Current ``HEAD`` in the local git repository, if available.
        commits_ahead: Number of commits on ``HEAD`` that are not in ``deployed_commit``.
        has_newer_commits: Whether ``commits_ahead`` is greater than zero.
        git_available: Whether git commands succeeded for the repository.
    """

    deployed_commit: str | None
    current_commit: str | None
    commits_ahead: int | None
    has_newer_commits: bool
    git_available: bool


def get_app_version() -> str:
    """Return the semantic application version from pyproject.toml or the installed package.

    Returns:
        Semantic version string such as ``0.2.0``.
    """
    try:
        with PYPROJECT_PATH.open("rb") as pyproject_file:
            data = tomllib.load(pyproject_file)
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        logger.warning("Could not determine application version from %s", PYPROJECT_PATH)

    try:
        return pkg_version(PROJECT_NAME)
    except PackageNotFoundError:
        logger.warning("Could not determine application version from installed package metadata")
        return "0.0.0"


def _git_rev_parse(revision: str) -> str | None:
    """Resolve a git revision to a full commit hash.

    Args:
        revision: Git revision such as ``HEAD`` or a commit hash.

    Returns:
        Full commit hash, or ``None`` when git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", revision],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=_GIT_CWD,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    commit = result.stdout.strip()
    return commit or None


@lru_cache(maxsize=1)
def get_git_commit_at_start() -> str | None:
    """Return the git ``HEAD`` commit cached at process startup.

    Used as the "running" commit baseline. Compared against a fresh ``HEAD`` read
    on the admin stats page to detect commits pulled since the process started.

    Returns:
        Full commit hash for ``HEAD`` at first call, or ``None`` when git is unavailable.
    """
    return _git_rev_parse("HEAD")


def get_current_git_commit() -> str | None:
    """Return the current git ``HEAD`` commit hash.

    Returns:
        Full commit hash, or ``None`` when git is unavailable.
    """
    return _git_rev_parse("HEAD")


def _count_commits_ahead(base_commit: str, head_commit: str) -> int | None:
    """Count commits reachable from ``head_commit`` but not ``base_commit``.

    Args:
        base_commit: Deployed commit hash.
        head_commit: Current repository ``HEAD`` hash.

    Returns:
        Number of commits ahead, or ``None`` when git cannot compute the range.
    """
    if base_commit == head_commit:
        return 0

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base_commit}..{head_commit}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=_GIT_CWD,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def format_short_commit(commit: str | None, length: int = 7) -> str:
    """Return a shortened commit hash for display.

    Args:
        commit: Full commit hash.
        length: Number of hex characters to keep.

    Returns:
        Short hash, or an em dash when ``commit`` is missing.
    """
    if not commit:
        return "—"
    return commit[:length]


def get_git_update_status(deployed_commit: str | None) -> GitUpdateStatus:
    """Compare the deployed commit against the current repository ``HEAD``.

    Args:
        deployed_commit: Commit hash recorded at deploy or process start.

    Returns:
        GitUpdateStatus describing whether newer commits are available locally.
    """
    current_commit = get_current_git_commit()
    if current_commit is None:
        return GitUpdateStatus(
            deployed_commit=deployed_commit,
            current_commit=None,
            commits_ahead=None,
            has_newer_commits=False,
            git_available=False,
        )

    if not deployed_commit:
        return GitUpdateStatus(
            deployed_commit=None,
            current_commit=current_commit,
            commits_ahead=0,
            has_newer_commits=False,
            git_available=True,
        )

    commits_ahead = _count_commits_ahead(deployed_commit, current_commit)
    has_newer = commits_ahead is not None and commits_ahead > 0
    return GitUpdateStatus(
        deployed_commit=deployed_commit,
        current_commit=current_commit,
        commits_ahead=commits_ahead,
        has_newer_commits=has_newer,
        git_available=True,
    )
