"""Application version and deployment metadata."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess  # nosec B404
import sys
import tomllib

from urbanlens.UrbanLens.settings.meta.app import DEFAULT_ROOT

logger = logging.getLogger(__name__)

PROJECT_NAME = "UrbanLens"
PYPROJECT_PATH = DEFAULT_ROOT.parent / "pyproject.toml"
_GIT_CWD = DEFAULT_ROOT.parent
MANAGE_PY = DEFAULT_ROOT / "urbanlens" / "manage.py"
_DJANGO_CWD = DEFAULT_ROOT.parent
_GIT_EXECUTABLE: str = shutil.which("git") or "git"


@dataclass(frozen=True, slots=True)
class GitUpdateStatus:
    """Deployed commit compared against current repository state."""

    deployed_commit: str | None
    current_commit: str | None
    upstream_commit: str | None
    commits_ahead: int | None
    has_newer_commits: bool
    git_available: bool
    remote_refreshed: bool


def get_app_version() -> str:
    r"""Return the app version from pyproject.toml or installed metadata."""
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
    """Resolve a git revision to a commit hash, or None."""
    try:
        result = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "rev-parse", revision],
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
    """Return ``HEAD`` at first call, cached for process lifetime."""
    return _git_rev_parse("HEAD")


def get_current_git_commit() -> str | None:
    """Return the current ``HEAD`` commit hash, or None."""
    return _git_rev_parse("HEAD")


def get_current_git_branch() -> str | None:
    """Return the current branch name, or None."""
    try:
        result = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=_GIT_CWD,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    branch = result.stdout.strip()
    return branch or None


@lru_cache(maxsize=1)
def _git_fetch() -> bool:
    """Refresh remote-tracking refs; cached for process lifetime."""
    try:
        remote_check = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "remote"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=_GIT_CWD,
        )
    except (subprocess.SubprocessError, OSError):
        logger.debug("git remotes unavailable; skipping fetch")
        return False

    if remote_check.returncode != 0 or not remote_check.stdout.strip():
        logger.debug("no git remotes configured; skipping fetch")
        return False

    logger.debug("running git fetch --quiet --prune")
    try:
        result = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "fetch", "--quiet", "--prune"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=_GIT_CWD,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("git fetch unavailable (%s); update status will use local refs only", exc)
        return False

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        logger.debug("git fetch failed (%s); update status will use local refs only", detail)
        return False

    return True


def get_upstream_git_commit() -> str | None:
    """Return the upstream tracking ref commit, or None."""
    return _git_rev_parse("@{u}")


def _count_commits_ahead(base_commit: str, head_commit: str) -> int | None:
    """Count commits in head_commit not in base_commit, or None."""
    if base_commit == head_commit:
        return 0

    try:
        result = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "rev-list", "--count", f"{base_commit}..{head_commit}"],
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


def apply_pending_migrations() -> tuple[bool, str]:
    """Apply pending migrations; return (ok, message)."""
    logger.info("Applying pending migrations")
    try:
        result = subprocess.run(  # nosec B603
            [sys.executable, str(MANAGE_PY), "migrate", "--noinput"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            cwd=_DJANGO_CWD,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("django migrate timed out after git pull", exc_info=True)
        return False, "Timed out while applying database migrations."
    except OSError:
        logger.warning("django migrate could not be started after git pull", exc_info=True)
        return False, "Could not start database migrations on this server."

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        return True, output or "Database migrations are up to date."

    logger.warning("django migrate failed with exit code %s: %s", result.returncode, output)
    return False, output or "Database migrations failed."


def _parent_process_command() -> str:
    """Return the parent process command line, or empty string."""
    try:
        raw = Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
    except OSError:
        return ""

    return raw.replace(b"\x00", b" ").decode(errors="replace").strip()


def trigger_development_app_reload() -> tuple[bool, str]:
    """Reload the app server after a code update; return (ok, message)."""
    logger.info("Attempting to reload the server...")
    parent_cmd = _parent_process_command()
    # SIGHUP only exists on Linux, where gunicorn runs.
    sighup = getattr(signal, "SIGHUP", 1)
    if "gunicorn" in parent_cmd:
        try:
            os.kill(os.getppid(), sighup)
        except OSError:
            logger.warning("could not signal gunicorn master to reload", exc_info=True)
            return False, "Could not signal the application server to reload."
        return True, "Application server reload requested."

    reload_trigger = Path(__file__)
    try:
        os.utime(reload_trigger, None)
    except OSError:
        logger.warning("could not touch %s to trigger development reload", reload_trigger, exc_info=True)
        return False, "Could not signal the development server to reload."

    return True, "Development server reload requested."


def pull_latest_git_code() -> tuple[bool, str]:
    """Pull current branch fast-forward-only; return (ok, message)."""
    try:
        result = subprocess.run(  # nosec B603
            [_GIT_EXECUTABLE, "pull", "--ff-only"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=_GIT_CWD,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        logger.warning("git pull timed out", exc_info=True)
        return False, "Timed out while pulling updates."
    except OSError:
        logger.warning("git pull could not be started", exc_info=True)
        return False, "Could not start git pull on this server."

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        return True, output or "Code is already up to date."

    logger.warning("git pull failed with exit code %s: %s", result.returncode, output)
    return False, output or "Git pull failed."


def format_short_commit(commit: str | None, length: int = 7) -> str:
    """Return a shortened commit hash, or a dash when missing."""
    if not commit:
        return "-"
    return commit[:length]


def _latest_reference_commit(
    deployed_commit: str | None,
    current_commit: str | None,
    upstream_commit: str | None,
) -> str | None:
    """Pick the known commit furthest ahead of the deployed baseline."""
    candidates: list[str] = []
    if current_commit:
        candidates.append(current_commit)
    if upstream_commit:
        candidates.append(upstream_commit)

    if not candidates:
        return None

    if not deployed_commit or len(candidates) == 1:
        return upstream_commit or current_commit

    best_commit = candidates[0]
    best_ahead = _count_commits_ahead(deployed_commit, best_commit) or 0
    for candidate in candidates[1:]:
        ahead = _count_commits_ahead(deployed_commit, candidate) or 0
        if ahead > best_ahead or (ahead == best_ahead and candidate == upstream_commit):
            best_commit = candidate
            best_ahead = ahead

    return best_commit


def get_git_update_status(deployed_commit: str | None) -> GitUpdateStatus:
    """Compare deployed commit against local and remote state."""
    remote_refreshed = _git_fetch()
    current_commit = get_current_git_commit()
    upstream_commit = get_upstream_git_commit()

    if current_commit is None and upstream_commit is None:
        return GitUpdateStatus(
            deployed_commit=deployed_commit,
            current_commit=None,
            upstream_commit=None,
            commits_ahead=None,
            has_newer_commits=False,
            git_available=False,
            remote_refreshed=remote_refreshed,
        )

    if not deployed_commit:
        return GitUpdateStatus(
            deployed_commit=None,
            current_commit=current_commit,
            upstream_commit=upstream_commit,
            commits_ahead=0,
            has_newer_commits=False,
            git_available=True,
            remote_refreshed=remote_refreshed,
        )

    reference_commit = _latest_reference_commit(deployed_commit, current_commit, upstream_commit)
    commits_ahead = _count_commits_ahead(deployed_commit, reference_commit) if reference_commit else None
    has_newer = commits_ahead is not None and commits_ahead > 0
    return GitUpdateStatus(
        deployed_commit=deployed_commit,
        current_commit=current_commit,
        upstream_commit=upstream_commit,
        commits_ahead=commits_ahead,
        has_newer_commits=has_newer,
        git_available=True,
        remote_refreshed=remote_refreshed,
    )
