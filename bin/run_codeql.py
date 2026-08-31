#!/usr/bin/env python3
"""Run CodeQL locally, thoroughly, and optionally as a pre-push gate.

GitHub already analyses pull requests (``.github/workflows/security.yml``) with
the default ``code-scanning`` suites. That is after the branch exists. This
script is the same analysis on this machine, so a finding shows up before a PR
does, and the default manual invocation is *broader* than CI: the
``security-and-quality`` suites, plus the ``local`` threat model (file, env,
CLI - not just HTTP) so management commands and parsers are in scope too.

Database creation plus analysis is minutes, not seconds, which is why the
pre-commit hook is a **pre-push** hook rather than a per-commit one. ``--gate``
reuses a previous database when the analysed tree has not changed, and uses
the same suites CI uses so a push fails on what the GitHub job would report.

Install the official bundle (CLI + precompiled queries) with ``--install``.
The GitHub CodeQL Action bundle is required - the standalone CLI zip does not
ship query packs, and analysis would then have nothing to run. JavaScript,
TypeScript, and GitHub Actions extraction also need ``node`` on PATH (bun is
not a substitute); without it, ``--languages python`` still works.

Usage:
    python bin/run_codeql.py              # exhaustive local scan
    python bin/run_codeql.py --install    # download the CLI bundle
    python bin/run_codeql.py --languages python
    python bin/run_codeql.py --gate       # pre-push: CI suites, reuse DB
    python bin/run_codeql.py --fast       # reuse the existing database
    python bin/run_codeql.py --verbose    # print note-level findings too
    python bin/run_codeql.py --all-queries
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

if os.name == "nt":
    import ctypes
    import winreg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_CLUSTER = REPO_ROOT / ".codeql" / "db"
RESULTS_DIR = REPO_ROOT / ".codeql" / "results"
STAMP_PATH = REPO_ROOT / ".codeql" / "stamp.json"
CI_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
LOCAL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config-local.yml"

#: CodeQL-action language ids. ``javascript`` includes TypeScript.
LANGUAGES = ("python", "javascript", "actions")

SUITE_BY_MODE = {
    "gate": "{lang}-code-scanning.qls",
    "local": "{lang}-security-and-quality.qls",
}

BUNDLE_REPO = "github/codeql-action"
CODEQL_RELEASES = f"https://api.github.com/repos/{BUNDLE_REPO}/releases/latest"
SKIP_ENV = "UL_SKIP_CODEQL"

#: Languages whose extractor shells out to Node.js (bun is not a substitute).
_JS_EXTRACTOR_LANGUAGES = frozenset({"javascript", "actions"})

#: Top-level ``finalised:`` in ``codeql-database.yml``. Nested keys are indented.
_FINALISED_RE = re.compile(r"^finalised:\s*(true|false)\s*$", re.MULTILINE)

#: Extra excludes for the JavaScript extractor, which walks the tree before
#: applying ``paths-ignore`` and has crashed on a Windows ``.venv/lib64``
#: junction (``FileSystemException: The file cannot be accessed by the system``).
_JS_INDEX_FILTERS = """\
exclude:.venv
exclude:.venv_windows
exclude:node_modules
exclude:.codeql
exclude:.git
exclude:pgsql
exclude:.mypy_cache
exclude:.hypothesis
exclude:__pycache__
exclude:.pytest_cache
exclude:media
exclude:logs
"""


def _require_https(url: str) -> str:
    """Return *url* if it is https, otherwise raise.

    Args:
        url: Candidate URL.

    Returns:
        The same URL.

    Raises:
        ValueError: If the scheme is not https.
    """
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-https URL: {url}")
    return url


def _default_install_dir() -> Path:
    """Return the per-user directory the bundle is extracted into."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Programs" / "CodeQL"
    return Path.home() / ".local" / "share" / "codeql"


def _candidate_binaries() -> list[Path]:
    """Return locations that might hold a CodeQL CLI executable."""
    names = ("codeql.exe", "codeql")
    roots: list[Path] = []
    env_home = os.environ.get("CODEQL_HOME")
    if env_home:
        roots.append(Path(env_home))
    roots.append(_default_install_dir())
    roots.append(Path.home() / "codeql")
    found: list[Path] = []
    for root in roots:
        for name in names:
            found.append(root / name)
            found.append(root / "codeql" / name)
    on_path = shutil.which("codeql")
    if on_path:
        found.insert(0, Path(on_path))
    return found


def _codeql_works(path: Path) -> bool:
    """Return True if *path* is a CodeQL CLI that can actually start.

    A partial extract leaves ``codeql.exe`` on disk without its bundled JRE,
    and ``codeql version`` then fails with a missing ``java.exe``.
    """
    try:
        result = subprocess.run([str(path), "version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_codeql() -> Path | None:
    """Return the CodeQL CLI executable, or ``None`` if it is not installed."""
    seen: set[Path] = set()
    for path in _candidate_binaries():
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file() and _codeql_works(path):
            return path
    return None


def _codeql_cmd(codeql: Path, *args: str) -> list[str]:
    """Build a CodeQL invocation."""
    return [str(codeql), *args]


def _run(codeql: Path, *args: str, check: bool = True, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a CodeQL command with the repo as cwd.

    Args:
        codeql: Path to the CLI executable.
        *args: Arguments following the executable.
        check: Raise if the process exits non-zero.
        extra_env: Additional environment variables for this process.

    Returns:
        The completed process.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        _codeql_cmd(codeql, *args),
        cwd=REPO_ROOT,
        check=check,
        text=True,
        env=env,
    )


def _platform_bundle_name() -> str:
    """Return the CodeQL-action asset name for this OS/arch."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows":
        return "codeql-bundle-win64.tar.gz"
    if system == "Darwin":
        if machine in {"arm64", "aarch64"}:
            return "codeql-bundle-osx64.tar.gz"
        return "codeql-bundle-osx64.tar.gz"
    return "codeql-bundle-linux64.tar.gz"


def _latest_bundle_asset() -> tuple[str, str]:
    """Return ``(tag, download_url)`` for the current CodeQL-action bundle.

    Prefers ``gh`` so an authenticated GitHub CLI session is used; urllib is
    the fallback when ``gh`` is not installed.

    Returns:
        Release tag and the platform-specific ``.tar.gz`` asset URL.

    Raises:
        RuntimeError: If the GitHub API response has no matching asset.
    """
    gh = shutil.which("gh")
    if gh:
        listing = subprocess.run(
            [gh, "release", "view", "--repo", BUNDLE_REPO, "--json", "tagName,assets"],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        payload = json.loads(listing.stdout)
        tag = str(payload["tagName"])
        want = _platform_bundle_name()
        for asset in payload["assets"]:
            if asset.get("name") == want:
                url = str(asset.get("url") or "")
                if url:
                    return tag, _require_https(url)
        return tag, ""
    request = urllib.request.Request(  # noqa: S310
        _require_https(CODEQL_RELEASES),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "urbanlens-codeql"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        payload = json.load(response)
    tag = str(payload["tag_name"])
    want = _platform_bundle_name()
    for asset in payload["assets"]:
        if asset["name"] == want:
            return tag, _require_https(str(asset["browser_download_url"]))
    raise RuntimeError(f"No {want} asset on {tag}")


def _add_to_user_path(directory: Path) -> None:
    """Append *directory* to the user PATH on Windows; print export elsewhere."""
    resolved = str(directory)
    path_now = os.environ.get("PATH", "")
    if resolved.lower() not in path_now.lower():
        os.environ["PATH"] = resolved + os.pathsep + path_now
    if os.name != "nt":
        print(f'Add to PATH: export PATH="{resolved}:$PATH"', flush=True)
        return

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
    try:
        current, _regtype = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        current = ""
        _regtype = winreg.REG_EXPAND_SZ
    parts = [part for part in str(current).split(";") if part]
    if resolved not in parts and resolved.rstrip("\\") not in parts:
        parts.append(resolved)
        winreg.SetValueEx(key, "Path", 0, _regtype, ";".join(parts))
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(hwnd_broadcast, wm_settingchange, 0, "Environment", 0, 5000, None)
        print(f"Added {resolved} to the user PATH. Open a new terminal for other tools to see it.")
    else:
        print(f"{resolved} is already on the user PATH.")


def _extract_bundle(archive: Path, dest: Path) -> Path:
    """Extract the CodeQL bundle and return the directory that contains ``codeql``.

    Args:
        archive: Downloaded ``.tar.gz``.
        dest: Directory to extract into (created if needed).

    Returns:
        Directory that contains the ``codeql`` / ``codeql.exe`` executable.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(dest, filter="data")
    for candidate in (dest / "codeql", dest):
        exe = candidate / ("codeql.exe" if os.name == "nt" else "codeql")
        if exe.is_file():
            return candidate
    matches = list(dest.rglob("codeql.exe" if os.name == "nt" else "codeql"))
    if matches:
        return matches[0].parent
    raise RuntimeError(f"Extracted {archive} into {dest} but could not find the codeql executable")


def _download_bundle(tag: str, url: str, archive: Path) -> None:
    """Download the platform bundle to *archive*, preferring ``gh`` when present."""
    gh = shutil.which("gh")
    if gh:
        print(f"Downloading {tag} with gh")
        subprocess.run(
            [
                gh,
                "release",
                "download",
                tag,
                "-R",
                BUNDLE_REPO,
                "-p",
                _platform_bundle_name(),
                "-D",
                str(archive.parent),
                "--clobber",
            ],
            check=True,
            timeout=1800,
        )
        return
    print(f"Downloading {url}")
    urllib.request.urlretrieve(_require_https(url), archive)  # noqa: S310


def install_codeql(*, force: bool = False) -> Path:
    """Download the official CodeQL bundle and put the CLI on PATH.

    Args:
        force: Re-download and replace an existing install.

    Returns:
        Path to the installed executable.
    """
    existing = find_codeql()
    if existing is not None and not force:
        print(f"CodeQL already installed at {existing}")
        subprocess.run([str(existing), "version"], check=True)
        _add_to_user_path(existing.parent)
        return existing

    dest = _default_install_dir()
    if dest.exists() and (force or not find_codeql()):
        print(f"Removing incomplete or previous install at {dest}")
        shutil.rmtree(dest)

    tag, url = _latest_bundle_asset()
    print(f"Installing CodeQL {tag} ({_platform_bundle_name()}) into {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    cache = dest.parent / "CodeQL-bundle"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / _platform_bundle_name()
    if not archive.is_file() or archive.stat().st_size < 100_000_000:
        _download_bundle(tag, url, archive)
    else:
        print(f"Reusing downloaded bundle at {archive}")
    if not archive.is_file():
        raise RuntimeError(f"Download finished but {archive} is missing")
    extracted = _extract_bundle(archive, dest)
    exe = extracted / ("codeql.exe" if os.name == "nt" else "codeql")
    if not _codeql_works(exe):
        raise RuntimeError(f"Extracted {exe} but `codeql version` failed - the bundle is incomplete")
    _add_to_user_path(extracted)
    print(f"CodeQL CLI: {exe}")
    subprocess.run([str(exe), "version"], check=True)
    return exe


def _source_stamp(languages: tuple[str, ...], mode: str, extra: str) -> str:
    """Hash the analysed tree plus the analysis mode, so reuse is safe.

    Args:
        languages: Languages that will be extracted.
        mode: ``gate`` or ``local``.
        extra: Extra distinguisher (query selection, threat model, ...).

    Returns:
        Hex digest. Changes when tracked files, languages, or mode change.
    """
    listing = subprocess.run(["git", "ls-files", "-s"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    payload = "\n".join(
        (
            listing.stdout,
            ",".join(languages),
            mode,
            extra,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_stamp() -> dict[str, str]:
    """Return the last successful analysis stamp, or an empty mapping."""
    if not STAMP_PATH.is_file():
        return {}
    try:
        payload = json.loads(STAMP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_stamp(stamp: str, mode: str, languages: tuple[str, ...]) -> None:
    """Record a successful analysis so ``--gate`` can skip a no-op rebuild."""
    STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STAMP_PATH.write_text(
        json.dumps({"stamp": stamp, "mode": mode, "languages": list(languages)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _yaml_finalised(text: str) -> bool:
    """Return True if CodeQL YAML records a finished extract.

    A failed JavaScript/Actions extract still writes ``codeql-database.yml``
    with ``finalised: false``. Reusing that directory makes ``database analyze``
    fail with "needs to be finalized".

    Args:
        text: Contents of ``codeql-database.yml``.

    Returns:
        True only when the top-level ``finalised`` key is ``true``.
    """
    match = _FINALISED_RE.search(text)
    return match is not None and match.group(1) == "true"


def _result_level(result: dict, rules: dict) -> str:
    """Return the SARIF level for one result, using the rule default if omitted."""
    rule_id = result.get("ruleId") or "unknown"
    rule = rules.get(rule_id) or {}
    return result.get("level") or (rule.get("defaultConfiguration") or {}).get("level") or "warning"


def _print_sarif(path: Path, *, verbose: bool = False, quiet: bool = False) -> int:
    """Print findings from a SARIF file and return how many were error/warning.

    Default output is a per-rule count plus each error/warning. Notes are
    counted in the summary and omitted from the line dump unless ``verbose``.

    Args:
        path: SARIF document produced by ``database analyze``.
        verbose: Print note-level findings as well.
        quiet: Print only the summary, not individual findings.

    Returns:
        Count of results whose level is ``error`` or ``warning``. A missing
        result level uses the rule's ``defaultConfiguration.level``, then
        SARIF's default of ``warning``.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts: Counter[tuple[str, str]] = Counter()
    lines: list[tuple[str, str]] = []
    actionable = 0
    notes = 0
    for run in payload.get("runs", []):
        rules = {rule.get("id"): rule for rule in run.get("tool", {}).get("driver", {}).get("rules", []) if rule.get("id")}
        for result in run.get("results", []):
            rule_id = result.get("ruleId") or "unknown"
            level = _result_level(result, rules)
            message = (result.get("message") or {}).get("text") or ""
            locations = result.get("locations") or [{}]
            phys = (locations[0].get("physicalLocation") or {}) if locations else {}
            uri = (phys.get("artifactLocation") or {}).get("uri") or path.name
            start = (phys.get("region") or {}).get("startLine") or 0
            help_uri = ((rules.get(rule_id) or {}).get("helpUri") or "") if verbose else ""
            suffix = f"  ({help_uri})" if help_uri else ""
            line = f"{uri}:{start}: {level}: {rule_id}: {message}{suffix}"
            counts[(level, rule_id)] += 1
            lines.append((level, line))
            if level == "note":
                notes += 1
            else:
                actionable += 1
    print(f"{path.name}: {actionable} error/warning, {notes} note")
    rank = {"error": 0, "warning": 1, "note": 2}
    for (level, rule_id), n in sorted(
        counts.items(),
        key=lambda item: (rank.get(item[0][0], 9), -item[1], item[0][1]),
    ):
        print(f"  {n:5d}  {level:8s}  {rule_id}")
    if quiet:
        return actionable
    for level, line in lines:
        if verbose or level != "note":
            print(line)
    if notes and not verbose:
        print(f"  ({notes} note(s) omitted; pass --verbose to print them)")
    return actionable


def _suite_args(language: str, mode: str, all_queries: bool) -> list[str]:
    """Return the query-pack arguments for one language.

    Args:
        language: CodeQL language id (``python``, ``javascript``, ``actions``).
        mode: ``gate`` (CI-matching) or ``local`` (broadest standard suites).
        all_queries: If true, run every query in the language's query pack.

    Returns:
        Arguments inserted after ``database analyze <db>``.
    """
    if all_queries:
        pack = {
            "python": "codeql/python-queries",
            "javascript": "codeql/javascript-queries",
            "actions": "codeql/actions-queries",
        }[language]
        return [pack]
    return [SUITE_BY_MODE[mode].format(lang=language)]


def _analyze_language(
    codeql: Path,
    language: str,
    mode: str,
    *,
    all_queries: bool,
    threat_local: bool,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Analyse one language database and print its findings.

    Args:
        codeql: CLI executable.
        language: CodeQL language id.
        mode: ``gate`` or ``local``.
        all_queries: Run the entire query pack rather than a suite.
        threat_local: Also treat local inputs (file/env/CLI) as taint sources.
        verbose: Print note-level findings and CodeQL progress.
        quiet: Print only the per-rule summary from SARIF.

    Returns:
        Count of error/warning findings. ``-1`` if the database is missing.
    """
    db = DB_CLUSTER / language
    if not _database_ready(language):
        yml = db / "codeql-database.yml"
        if yml.is_file():
            print(f"Skipping {language}: database is not finalised", file=sys.stderr)
        else:
            print(f"No database for {language} at {db} - skipping")
        return -1
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sarif = RESULTS_DIR / f"{language}.sarif"
    args = [
        "database",
        "analyze",
        str(db),
        *_suite_args(language, mode, all_queries),
        "--format=sarif-latest",
        f"--output={sarif}",
        "--sarif-add-query-help=true",
        "--threads=0",
        "--download",
    ]
    if not verbose:
        args.append("--quiet")
    if threat_local:
        args.append("--threat-model=local")
    print(f"Analysing {language} -> {sarif}")
    try:
        _run(codeql, *args)
    except subprocess.CalledProcessError as exc:
        print(f"Failed to analyse {language} (exit {exc.returncode})", file=sys.stderr)
        return -1
    return _print_sarif(sarif, verbose=verbose, quiet=quiet)


def _database_ready(language: str) -> bool:
    """Return True if a previous extract for *language* finished and was finalised."""
    yml = DB_CLUSTER / language / "codeql-database.yml"
    if not yml.is_file():
        return False
    try:
        return _yaml_finalised(yml.read_text(encoding="utf-8"))
    except OSError:
        return False


def _node_bindir() -> Path | None:
    """Return the directory containing a Node.js executable, if any.

    The JavaScript/TypeScript extractor (also used for GitHub Actions) requires
    ``node`` on PATH. This checkout's frontend tooling is bun, which does not
    provide that binary.
    """
    which = shutil.which("node")
    if which:
        return Path(which).parent
    names = ("node.exe", "node")
    roots = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
        Path.home() / "nodejs",
    ]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return root
    return None


def _extractor_env() -> dict[str, str]:
    """Environment for database creation, including Node.js when present."""
    env = {"LGTM_INDEX_FILTERS": _JS_INDEX_FILTERS}
    node_dir = _node_bindir()
    if node_dir is not None:
        env["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
    return env


def create_databases(codeql: Path, languages: tuple[str, ...], config: Path, *, rebuild: bool = False) -> None:
    """(Re)build per-language databases under ``.codeql/db``.

    Databases are created one language at a time so an extractor crash in
    JavaScript (which the Actions extractor also uses) does not discard a
    finished Python database.

    Args:
        codeql: CLI executable.
        languages: Languages to extract.
        config: Code scanning config (paths / paths-ignore).
        rebuild: Recreate databases that already exist.
    """
    DB_CLUSTER.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    extra_env = _extractor_env()
    node_dir = _node_bindir()
    if node_dir is None and any(lang in _JS_EXTRACTOR_LANGUAGES for lang in languages):
        print(
            "Node.js is not on PATH. CodeQL's JavaScript/TypeScript extractor (and the Actions extractor, which uses it) need `node`. Install a Node LTS and re-run, or analyse Python only with: python bin/run_codeql.py --languages python",
            file=sys.stderr,
        )
    for language in languages:
        if not rebuild and _database_ready(language):
            print(f"Reusing existing {language} database")
            continue
        if language in _JS_EXTRACTOR_LANGUAGES and node_dir is None:
            print(f"Skipping {language} database creation (Node.js is required)", file=sys.stderr)
            failed.append(language)
            continue
        db = DB_CLUSTER / language
        print(f"Creating {language} database in {db}")
        try:
            _run(
                codeql,
                "database",
                "create",
                str(db),
                f"--language={language}",
                f"--source-root={REPO_ROOT}",
                f"--codescanning-config={config}",
                "--overwrite",
                "--threads=0",
                extra_env=extra_env,
            )
        except subprocess.CalledProcessError as exc:
            print(f"Failed to create the {language} database (exit {exc.returncode})", file=sys.stderr)
            failed.append(language)
    if failed:
        print(f"Database creation failed for: {', '.join(failed)}", file=sys.stderr)
    ready = [lang for lang in languages if _database_ready(lang)]
    if not ready:
        raise RuntimeError("No CodeQL databases were created")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Argument list without the program name. ``None`` uses ``sys.argv``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true", help="Download the official CodeQL bundle and exit")
    parser.add_argument("--force", action="store_true", help="With --install, replace an existing CodeQL install")
    parser.add_argument("--gate", action="store_true", help="Pre-push mode: CI suites, reuse DB when the tree is unchanged")
    parser.add_argument("--fast", action="store_true", help="Reuse finalised databases even when the tree has changed (findings may be stale)")
    parser.add_argument("--rebuild", action="store_true", help="Recreate databases even if a previous extract finished")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print note-level findings and CodeQL progress messages",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only per-rule counts, not individual findings",
    )
    parser.add_argument(
        "--all-queries",
        action="store_true",
        help="Run every query in each language pack, including experimental ones",
    )
    parser.add_argument(
        "--languages",
        default=",".join(LANGUAGES),
        help="Comma-separated CodeQL languages (default: python,javascript,actions)",
    )
    parser.add_argument(
        "--skip-if-missing",
        action="store_true",
        help="Exit 0 when the CLI is not installed (pre-push default via --gate)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Install and/or run CodeQL.

    Args:
        argv: Argument list without the program name.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    if os.environ.get(SKIP_ENV):
        print(f"{SKIP_ENV} is set; skipping CodeQL.")
        return 0

    if args.install:
        install_codeql(force=args.force)
        return 0

    languages = tuple(part.strip() for part in args.languages.split(",") if part.strip())
    mode = "gate" if args.gate else "local"
    extra = "all" if args.all_queries else "suites"
    extra += "+local-threat" if mode == "local" else ""
    stamp = _source_stamp(languages, mode, extra)

    codeql = find_codeql()
    if codeql is None:
        if args.gate or args.skip_if_missing:
            print("CodeQL CLI not found; skipping. Install with: python bin/run_codeql.py --install")
            print("CI still runs CodeQL on pull requests (.github/workflows/security.yml).")
            return 0
        print("CodeQL CLI not found. Install with: python bin/run_codeql.py --install", file=sys.stderr)
        return 2

    previous = _load_stamp()
    tree_matches_last_scan = previous.get("stamp") == stamp and previous.get("mode") == mode and DB_CLUSTER.is_dir()
    if args.gate and tree_matches_last_scan:
        print("CodeQL: analysed tree unchanged since the last successful scan; skipping rebuild.")
        return 0

    config = CI_CONFIG if mode == "gate" else LOCAL_CONFIG
    # A database is a snapshot of the tree it was extracted from, so reusing one
    # after an edit analyses the old code and reports on it as though it were
    # current - a scan that cannot fail on anything you just wrote. Reaching
    # this line at all means the stamp did not match, i.e. the tree moved;
    # `_database_ready` only asks whether an extract *finished*, never what it
    # finished on, so it cannot make this call itself. --fast is the explicit
    # opt-in to analysing a stale database.
    reuse_is_safe = tree_matches_last_scan or args.fast
    if args.fast and not tree_matches_last_scan:
        print("Reusing finalised databases (--fast): findings reflect the tree they were extracted from, not the current one.")
    create_databases(codeql, languages, config, rebuild=args.rebuild or not reuse_is_safe)

    threat_local = mode == "local"
    verbose = bool(args.verbose)
    quiet = bool(args.quiet) and not verbose
    total_actionable = 0
    missing = 0
    for language in languages:
        count = _analyze_language(
            codeql,
            language,
            mode,
            all_queries=args.all_queries,
            threat_local=threat_local,
            verbose=verbose,
            quiet=quiet,
        )
        if count < 0:
            missing += 1
            continue
        total_actionable += count

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"SARIF written under {RESULTS_DIR}")
    if missing:
        print(f"{missing} language(s) had no finalised database.", file=sys.stderr)
    if total_actionable:
        print(f"CodeQL found {total_actionable} error/warning finding(s).")
        return 1
    if missing:
        print("No language databases were produced for one or more requested languages.", file=sys.stderr)
        return 2
    _write_stamp(stamp, mode, languages)
    print("CodeQL found no error/warning findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
