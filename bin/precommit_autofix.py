#!/usr/bin/env python3
"""Apply safe auto-fixes to the staged files, stage them, and gate on the rest.

Why this exists
---------------
pre-commit calls a hook FAILED when the worktree no longer matches the index
afterwards ("files were modified by this hook"). Every auto-fixing hook
therefore costs a wasted commit: the first `git commit` fixes and fails, the
second succeeds. Running the fixers here and staging what they changed leaves
the diff pre-commit compares unchanged, so a normal commit passes on the first
run with the fixes already in it.

This replaces five hooks that rewrote files without staging them: ruff --fix,
ruff-format, end-of-file-fixer, trailing-whitespace and mixed-line-ending.

Why partially staged files are skipped
--------------------------------------
pre-commit stashes unstaged changes before hooks run and restores them
afterwards with `git apply`. If a fix is staged and that restore conflicts,
pre-commit's rollback does `git checkout -- .`, which restores the worktree
from an index that already contains the fix; the re-apply then fails a second
time and the unstaged work is left only in pre-commit's patch file. Leaving
such files completely untouched is what makes that impossible. Inside a hook
`git diff` is already empty -- the stash has happened -- so the affected files
are identified from the patch pre-commit just wrote.

The lint gate
-------------
Files this script fixed are held to the full `ruff check`: every safe fix has
just been applied, so anything left needs a human. Files it had to skip are
held only to violations ruff cannot fix at all, because failing a commit over
something the script was not allowed to fix is exactly the friction this is
meant to remove.

Formatting width is ruff's business, not this script's: test directories carry
their own ``.ruff.toml`` setting ``line-length = 120``, which ruff finds by
walking up from each file. Keeping a second copy of that rule here is how
``ruff format --check src`` in CI came to disagree with the commit path.

It never applies ruff's unsafe fixes, and never fails a commit because of an
error in itself. In particular it does not delete code: unused imports (F401)
and stale ``noqa`` directives (RUF100) are left alone deliberately, since a
binding kept for a future caller and a comment explaining a suppression are
both information a formatter should not be allowed to discard.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

# A stash patch is written immediately before the first hook runs, so this only
# has to cover the hooks scheduled ahead of this one.
PATCH_WINDOW_SECONDS = 900

TIMEOUT_SECONDS = 120

#: `git add` contends with any other git process in this checkout.
LOCK_RETRIES = 4
LOCK_RETRY_SECONDS = 0.25


# Mirrors the `files:` pattern in .pre-commit-config.yaml, as a second guard in
# case this script is ever invoked by hand.
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
    ".txt",
}


def _run(*args: str, timeout: int = TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


# --------------------------------------------------------------------------
# locating ruff
# --------------------------------------------------------------------------


def _ruff_command() -> list[str] | None:
    """The ruff to use, or None if this checkout has none installed.

    `git commit` from a shell with no activated virtualenv does not necessarily
    have ruff on PATH, so the project venv is checked explicitly.
    """
    found = shutil.which("ruff")
    if found:
        return [found]
    root = Path(__file__).resolve().parent.parent
    for candidate in (root / ".venv/bin/ruff", root / ".venv/Scripts/ruff.exe"):
        if candidate.is_file():
            return [str(candidate)]
    probe = _run(sys.executable, "-m", "ruff", "--version", timeout=30)
    if probe.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    return None


# --------------------------------------------------------------------------
# which files pre-commit stashed
# --------------------------------------------------------------------------


def _patch_dir() -> Path:
    home = os.environ.get("PRE_COMMIT_HOME")
    if home:
        return Path(home)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pre-commit"


def _precommit_pid() -> int | None:
    """The pid of the pre-commit process that invoked this hook, if knowable.

    pre-commit names its stash patch `patch<unixtime>-<its pid>`, so matching on
    that pid identifies this run's patch exactly rather than by a time window.
    """
    pid = os.getppid()
    for _ in range(12):  # bounded walk up the ancestry
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        if "pre-commit" in cmdline or "pre_commit" in cmdline:
            return pid
        try:
            # ppid is the 4th field, after a comm field that may contain spaces
            pid = int(stat[stat.rindex(")") + 1 :].split()[1])
        except (ValueError, IndexError):
            return None
        if pid <= 1:
            return None
    return None


def stashed_paths() -> tuple[bool, set[str]]:
    """Files that had unstaged changes when this commit started.

    Returns:
        (uncertain, paths). When `uncertain` is true the caller must not stage
        anything, because a stash it cannot see may exist.
    """
    now = time.time()
    own_pid = _precommit_pid()
    try:
        candidates = list(_patch_dir().glob("patch*-*"))
    except OSError:
        return True, set()

    paths: set[str] = set()
    uncertain = False
    for patch in candidates:
        match = re.fullmatch(r"patch(\d+)-(\d+)", patch.name)
        if not match:
            continue
        stamp, pid = int(match.group(1)), int(match.group(2))
        if own_pid is not None:
            if pid != own_pid:
                continue
        elif not (now - PATCH_WINDOW_SECONDS <= stamp <= now + 60):
            continue
        try:
            body = patch.read_text(errors="replace")
        except OSError:
            uncertain = True
            continue
        for line in body.splitlines():
            # `diff --git` heads every file in the patch, including binary ones,
            # which carry no ---/+++ pair at all. Parsing only those would miss
            # a stashed binary and let the fixer stage over it.
            if line.startswith("diff --git "):
                name = _diff_header_path(line)
                if name is None:
                    uncertain = True  # unparsable: assume it could be anything
                else:
                    paths.add(name)
    return uncertain, paths


def _diff_header_path(line: str) -> str | None:
    r"""The b-side path from a `diff --git a/X b/X` line, or None if unreadable.

    Git C-quotes any path with a non-ASCII or special character
    (`diff --git "a/caf\303\251.py" "b/caf\303\251.py"`), so splitting on
    whitespace and stripping `b/` silently produces a name that matches nothing
    - and a file the fixer then treats as safe to rewrite and stage, which is
    exactly the case the skip list exists to prevent.
    """
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        # Quoted: the two paths are separate quoted strings.
        try:
            closing = rest.index('" "', 1)
        except ValueError:
            return None
        quoted = rest[closing + 3 :].rstrip()
        if not quoted.endswith('"'):
            return None
        try:
            decoded = quoted[:-1].encode("latin-1", "strict").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return None
        return decoded[2:] if decoded.startswith("b/") else None
    # Unquoted, both sides name the same path: `a/P b/P`. Splitting on " b/"
    # picks the wrong point for a path that itself contains " b/" (git does not
    # quote a plain space), so derive the length instead and verify the result
    # reconstructs the line. A rename header (a/X b/Y) fails that check and
    # returns None, which the caller treats as "cannot be sure" - the safe way
    # to be wrong.
    rest = rest.rstrip()
    if not rest.startswith("a/"):
        return None
    width = (len(rest) - 5) // 2
    if width <= 0:
        return None
    name = rest[2 : 2 + width]
    return name if rest == f"a/{name} b/{name}" else None


# --------------------------------------------------------------------------
# fixers
# --------------------------------------------------------------------------


def fix_whitespace(path: Path) -> None:
    """Normalise line endings to LF, strip trailing whitespace, end with one newline."""
    try:
        original = path.read_bytes()
    except OSError:
        return
    if b"\x00" in original:  # binary, never touch
        return
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        return
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    fixed = "".join(f"{line.rstrip()}\n" for line in lines)
    encoded = fixed.encode("utf-8")
    if encoded != original:
        try:
            path.write_bytes(encoded)
        except OSError:
            return


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


# --------------------------------------------------------------------------
# lint gate
# --------------------------------------------------------------------------


def unfixable_only(ruff: list[str], paths: list[str]) -> str:
    """`ruff check` output limited to violations ruff has no fix for."""
    result = _run(*ruff, "check", "--force-exclude", "--output-format", "json", "--quiet", *paths)
    try:
        items = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return ""  # unparsable: do not block the commit on it
    lines = []
    for item in items:
        if not isinstance(item, dict) or item.get("fix") is not None:
            continue
        name = item.get("filename") or ""
        # ruff reports absolute paths in json; match the plain-text form
        with contextlib.suppress(ValueError):
            name = os.path.relpath(name)
        row = (item.get("location") or {}).get("row")
        lines.append(f"{name}:{row}: {item.get('code')} {item.get('message')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------


def sweep_mode(ruff: list[str] | None, *, write: bool) -> int:
    """Apply (or report) the formatter across every tracked Python file.

    The commit path only ever sees staged files, so nothing there would notice a
    file that drifted out of format some other way (a rebase, a merge
    resolution, an edit made with the hooks skipped). This is that sweep, and it
    applies the same two widths, from the same code, so the two cannot disagree.

    Args:
        ruff: The resolved ruff command, or None if none is installed.
        write: Rewrite drifted files when true; only report them when false.

    Returns:
        0 when the tree is formatted (or was just formatted), 1 on drift found
        in report mode, or if ruff is missing.
    """
    label = "--format" if write else "--check"
    if ruff is None:
        print(f"autofix {label}: ruff not found (is the venv installed?)")
        return 1
    listing = _run("git", "ls-files", "-z", "--", "*.py", timeout=60)
    if listing.returncode != 0:
        print(f"autofix {label}: could not list tracked files")
        return 1
    targets = [Path(name) for name in listing.stdout.split("\0") if name]

    extra = [] if write else ["--check"]
    result = _run(*ruff, "format", *extra, "--force-exclude", *[str(t) for t in targets], timeout=300)

    # ruff format exits 0 when clean, 1 from --check when files would change, and
    # 2 when it could not run at all (bad config, unreadable file). Treating
    # anything non-zero as "drift" would be wrong; treating 2 as success would
    # make this gate pass precisely when it is broken, which is worse.
    if result.returncode not in (0, 1):
        print(f"autofix {label}: ruff format could not run (exit {result.returncode})")
        print(result.stderr.strip() or result.stdout.strip())
        return 1

    if write:
        # In write mode ruff prints only a summary; there is no per-file line to
        # count, so echo what it said rather than inventing a number.
        summary = next((ln for ln in result.stdout.splitlines() if "reformatted" in ln or "unchanged" in ln), "")
        print(f"autofix --format: {summary or 'nothing to do'}")
        return 0

    touched = [line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("Would reformat: ")]
    if touched:
        print("\n".join(f"Would reformat: {name}" for name in touched))
        print(f"\n{len(touched)} file(s) would be reformatted. Fix with: bun run format")
        return 1
    return 0


def _stage(paths: list[str]) -> str | None:
    """`git add` the paths, retrying briefly past a held index lock.

    This repo is worked in by more than one session at a time, so `index.lock`
    can be held by an unrelated `git` for a moment. Failing on the first attempt
    would leave correct fixes unstaged and hand the developer back the two-run
    behaviour this script exists to remove.

    Returns:
        None on success, else the last error message.
    """
    error = ""
    for attempt in range(LOCK_RETRIES):
        result = _run("git", "add", "--", *paths)
        if result.returncode == 0:
            return None
        error = result.stderr.strip()
        if "index.lock" not in error:
            break  # not contention; retrying will not help
        time.sleep(LOCK_RETRY_SECONDS * (attempt + 1))
    return error


def _already_staged() -> set[str] | None:
    """Paths with staged changes, or None if that cannot be determined.

    During a real commit every path pre-commit passes is staged, so this changes
    nothing there. It matters for `pre-commit run --all-files` (which is what
    `bun run pre-commit` and `bun run test:full` invoke): without it the hook
    would `git add` every file it touched anywhere in the tree, quietly staging
    work the developer had not chosen to commit.
    """
    result = _run("git", "diff", "--cached", "--name-only", "-z", timeout=60)
    if result.returncode != 0:
        return None
    return {name for name in result.stdout.split("\0") if name}


def main(argv: list[str]) -> int:
    if "--check" in argv or "--format" in argv:
        return sweep_mode(_ruff_command(), write="--format" in argv)

    uncertain, stashed = stashed_paths()
    if uncertain:
        print("autofix: could not confirm which files have unstaged changes; staging nothing")

    skipped: list[str] = []
    targets: list[Path] = []
    for name in argv:
        path = Path(name)
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if uncertain or name in stashed:
            skipped.append(name)
        else:
            targets.append(path)

    ruff = _ruff_command()
    if ruff is None:
        # Failing loudly rather than passing: `language: system` means this hook
        # uses whatever is installed, so a missing ruff silently removes the
        # lint and format gate from every commit until someone notices.
        print("autofix: ruff not found. Run `uv sync`, or commit with --no-verify if you know why it is missing.")
        return 1

    before = {path: _read(path) for path in targets}

    python_targets = [str(p) for p in targets if p.suffix == ".py"]
    if ruff and python_targets:
        # --force-exclude is required: for paths passed explicitly ruff otherwise
        # ignores pyproject's exclude list and would reformat every settings/ and
        # migrations/ file in the repo.
        # Safe fixes only -- --unsafe-fixes can change what the code does.
        _run(*ruff, "check", "--fix", "--exit-zero", "--force-exclude", "--quiet", *python_targets)
        _run(*ruff, "format", "--force-exclude", "--quiet", *python_targets)

    for path in targets:
        fix_whitespace(path)

    changed = [str(p) for p in targets if _read(p) != before[p]]
    staged_already = _already_staged()
    if staged_already is not None:
        unstaged_targets = [name for name in changed if name not in staged_already]
        if unstaged_targets:
            print(f"autofix: fixed but left unstaged (they had nothing staged): {', '.join(sorted(unstaged_targets))}")
        changed = [name for name in changed if name in staged_already]
    if changed:
        staged = _stage(changed)
        if staged is not None:
            print(f"autofix: could not stage fixes ({staged}); commit will report them")
        else:
            print(f"autofix: fixed and staged {len(changed)} file(s): {', '.join(sorted(changed))}")

    if skipped:
        print(
            f"autofix: not touched, because they have unstaged changes and fixing them could cost you that work: {', '.join(sorted(skipped))}",
        )

    problems = []
    # Files that were fixed: every safe fix has been applied, so hold them to
    # the full check.
    if python_targets:
        full = _run(*ruff, "check", "--force-exclude", "--quiet", *python_targets)
        if full.returncode not in (0, 1):
            print(f"autofix: ruff check did not run cleanly: {full.stderr.strip()}")
        elif full.stdout.strip():
            problems.append(full.stdout.strip())
    # Files that were skipped: only block on what ruff could not have fixed
    # anyway, never on the fixes this script was not allowed to apply.
    skipped_python = [name for name in skipped if name.endswith(".py")]
    if skipped_python:
        remaining = unfixable_only(ruff, skipped_python)
        if remaining:
            problems.append(remaining)

    if problems:
        print("\n" + "\n".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # never fail a commit because this script broke
        print(f"autofix: skipped after an internal error: {exc!r}")
        sys.exit(0)
