#!/usr/bin/env python3
"""
Open UrbanLens's release pull request on Forgejo, and publish a release once it is merged.

release-please has no Forgejo backend, so this does its two jobs with git-cliff (README.md,
"CI/CD and Releases"); it is REData's ``bin/release.py`` with UrbanLens's version rule. Run
``pr`` after a merge into ``main``, and ``publish <PR#>`` after merging the release pull
request, then ``pr`` again. Each is idempotent.

``pr``: read the conventional commits on ``main`` since ``v<current version>`` with git-cliff
(``cliff.toml``), pick the next version and open or update the Forgejo pull request
"chore(main): release X.Y.Z" from ``chore/release-main``. UrbanLens stays 0.x, with
release-please's bump-minor-pre-major: a breaking change or a feature bumps the minor,
anything else the patch. A ``Release-As: X.Y.Z`` line in a commit message since the tag
names the version instead, as it did for release-please. It bumps the files release-please
kept: ``.release-please-manifest.json``, ``pyproject.toml``, ``package.json``, ``uv.lock``
(UrbanLens's own entry) and ``CHANGELOG.md`` (a new section in release-please's format).
Nothing to release - only chores, tests, CI, refactors since the tag, and no ``Release-As`` -
opens nothing.

``.release-please-manifest.json`` records the last release. A release branch may already
carry the version it is heading for in the other files (``release/v_0_9_0`` records 0.9.0
before 0.9.0 is released); the bump writes the new version over whatever they record, as
long as that is not above it.

``publish <PR#>``: for a merged release pull request, tag its merge commit ``vX.Y.Z`` on
Forgejo and publish a Forgejo release whose notes are that version's CHANGELOG section.

git-cliff runs as ``uv tool run --from git-cliff==2.13.1`` (``GIT_CLIFF``), so the version is
exact and nothing is installed system-wide; uv caches the wheel after the first run.

Usage:
    bin/release.py pr [--dry-run]
    bin/release.py publish <PR#>
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import datetime as dt
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
GIT_CLIFF = "git-cliff==2.13.1"
GIT_CLIFF_TIMEOUT = 600
TARGET = "main"
RELEASE_BRANCH = "chore/release-main"
TITLE = "chore(main): release {version}"
TITLE_RE = re.compile(r"^chore\(main\): release (\d+\.\d+\.\d+)$")
PRIVATE_REF = "refs/release-tool/main"

CHANGELOG = "CHANGELOG.md"
MANIFEST = ".release-please-manifest.json"
#: Files that record UrbanLens's version, besides CHANGELOG.md; a release PR edits exactly these.
VERSION_FILES = (MANIFEST, "pyproject.toml", "package.json", "uv.lock")
BUMP_FILES = frozenset({CHANGELOG, *VERSION_FILES})
#: The package name uv.lock records UrbanLens under.
LOCK_NAME = "urbanlens"

#: CHANGELOG sections in order, as cliff.toml's groups name them (minus their sort comments).
BREAKING = "⚠ BREAKING CHANGES"
SECTIONS = ("Features", "Bug Fixes", "Performance Improvements", "Reverts", "Documentation", "Dependencies")
#: The section whose entries bump the minor, as a breaking change does (bump-minor-pre-major).
MINOR_SECTIONS = frozenset({"Features"})
#: release-please's override: a ``Release-As: X.Y.Z`` line in a commit message names the version.
RELEASE_AS_RE = re.compile(r"^release-as:\s*(\d+\.\d+\.\d+)\s*$", re.IGNORECASE | re.MULTILINE)
#: Forgejo releases are marked pre-release, as release-please's config had it, while UrbanLens is 0.x.
PRERELEASE = True


class ReleaseError(RuntimeError):
    """The release step cannot go on; nothing after the failing step happened."""


def _sibling(name: str) -> ModuleType:
    """Import ``bin/<name>.py`` from this checkout, whether run as a script or loaded by path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


forgejo_api = _sibling("forgejo_api")


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise ReleaseError(f"not an X.Y.Z version: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_version(current: str, *, minor: bool) -> str:
    """
    The version after ``current``: below 1.0.0, ``minor`` (a breaking change or a feature) bumps the minor, anything else the patch.

    That is release-please's bump-minor-pre-major. UrbanLens stays 0.x until the
    maintainer decides otherwise, so this never leaves 0.x and refuses a current version that already has.
    """
    major, minor_part, patch = parse_version(current)
    if major != 0:
        raise ReleaseError(f"current version {current} is not 0.x; UrbanLens stays 0.x until the maintainer decides otherwise")
    return f"0.{minor_part + 1}.0" if minor else f"0.{minor_part}.{patch + 1}"


def bumps_minor(entries: Iterable[Entry]) -> bool:
    """Whether these entries bump the minor: a breaking change or a feature among them."""
    return any(e.breaking or e.section in MINOR_SECTIONS for e in entries)


def release_as(messages: str, current: str) -> str | None:
    """
    The version the newest ``Release-As:`` line in ``messages`` (newest first) names, or None.

    One that is not above ``current`` is ignored with a note, as it can no longer apply; one
    that leaves 0.x stops the release.
    """
    for match in RELEASE_AS_RE.finditer(messages):
        version = match.group(1)
        if parse_version(version)[0] != 0:
            raise ReleaseError(f"a commit asks for Release-As: {version}; UrbanLens stays 0.x until the maintainer decides otherwise")
        if parse_version(version) <= parse_version(current):
            print(f"release: ignoring Release-As: {version}, which is not above {current}")
            continue
        return version
    return None


def without_version(path: str, text: str) -> object:
    """Parse a version file and drop UrbanLens's own version, leaving everything a bump must not touch."""
    if path == MANIFEST:
        return sorted(json.loads(text))  # its values are the versions; its keys must stay
    if path == "package.json":
        doc = json.loads(text)
        doc.pop("version", None)
        return doc
    doc = tomllib.loads(text)
    if path == "pyproject.toml":
        doc.get("project", {}).pop("version", None)
    else:  # uv.lock
        for package in doc.get("package", []):
            if package.get("name") == LOCK_NAME:
                package.pop("version", None)
    return doc


def recorded_version(path: str, text: str) -> str:
    """UrbanLens's version as ``path`` (one of VERSION_FILES) records it."""
    if path == MANIFEST:
        return str(json.loads(text)["."])
    if path == "package.json":
        return str(json.loads(text)["version"])
    doc = tomllib.loads(text)
    if path == "pyproject.toml":
        return str(doc["project"]["version"])
    return str(next(p["version"] for p in doc["package"] if p.get("name") == LOCK_NAME))


#: How each file spells the version line, so a bump edits the text and keeps the formatting.
_VERSION_PATTERNS = {
    MANIFEST: r'("\."\s*:\s*"){old}(")',
    "package.json": r'(^\s*"version"\s*:\s*"){old}(")',
    "pyproject.toml": r'(^version\s*=\s*"){old}(")',
    "uv.lock": rf'(^\[\[package\]\]\nname = "{LOCK_NAME}"\nversion = "){{old}}(")',
}


def bump_text(path: str, text: str, new: str) -> str:
    """
    ``text`` of ``path`` with UrbanLens's version set to ``new`` and nothing else changed.

    The file may already record ``new`` (a release branch that carried it ahead of the
    release), or any version below it; one above ``new`` stops the bump.
    """
    old = recorded_version(path, text)
    if parse_version(old) > parse_version(new):
        raise ReleaseError(f"{path} records {old}, which is above the release's {new}")
    pattern = re.compile(_VERSION_PATTERNS[path].format(old=re.escape(old)), re.MULTILINE)
    bumped, count = pattern.subn(rf"\g<1>{new}\g<2>", text, count=1)
    if count != 1:
        raise ReleaseError(f"{path} does not record version {old} where expected")
    if recorded_version(path, bumped) != new or without_version(path, bumped) != without_version(path, text):
        raise ReleaseError(f"bumping {path} would change more than UrbanLens's version")
    return bumped


# --------------------------------------------------------------------------
# The changelog
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One CHANGELOG line: a conventional commit git-cliff kept."""

    section: str
    scope: str
    text: str
    sha: str
    breaking: str = ""  # the breaking-change description, when the commit is one

    def line(self, text: str | None = None) -> str:
        scope = f"**{self.scope}:** " if self.scope else ""
        return f"* {scope}{text if text is not None else self.text} ([{self.sha[:7]}]({forgejo_api.WEB_URL}/commit/{self.sha}))"


def entries_from_context(context: Sequence[dict[str, Any]]) -> list[Entry]:
    """
    The CHANGELOG entries in git-cliff's ``--context`` output, oldest first, duplicates dropped.

    A commit rebased onto two branches, or kept on a branch and squashed onto another, lands
    twice with the same words; it is listed once, under its oldest sha.
    """
    entries: list[Entry] = []
    seen: set[tuple[str, str, str]] = set()
    for release in context:
        for commit in release.get("commits") or []:
            # cliff.toml names a group "<!-- 0 -->Features"; the comment only sorts it.
            section = (commit.get("group") or "").rpartition("-->")[2].strip()
            text = (commit.get("message") or "").split("\n", 1)[0].strip()
            breaking = (commit.get("breaking_description") or text) if commit.get("breaking") else ""
            if (section not in SECTIONS and not breaking) or not text:
                continue
            key = (section, (commit.get("scope") or "").casefold(), text.casefold())
            if key in seen:
                continue
            seen.add(key)
            entries.append(Entry(section, commit.get("scope") or "", text, str(commit["id"]), breaking))
    return entries


def _sort_key(entry: Entry, text: str) -> tuple[str, str]:
    """release-please's order: by scope, or the text when there is none, case-insensitively."""
    return ((entry.scope or text).casefold(), text.casefold())


def render_section(version: str, previous: str, date: dt.date, entries: Iterable[Entry]) -> str:
    """The CHANGELOG.md section for ``version``, in release-please's format, ending in one newline."""
    entries = list(entries)
    groups: list[tuple[str, list[str]]] = []
    breaking = sorted((e for e in entries if e.breaking), key=lambda e: _sort_key(e, e.breaking))
    if breaking:
        groups.append((BREAKING, [e.line(e.breaking) for e in breaking]))
    for section in SECTIONS:
        lines = [e.line() for e in sorted((e for e in entries if e.section == section), key=lambda e: _sort_key(e, e.text))]
        if lines:
            groups.append((section, lines))
    out = f"## [{version}]({forgejo_api.WEB_URL}/compare/v{previous}...v{version}) ({date.isoformat()})\n"
    for name, lines in groups:
        out += f"\n\n### {name}\n\n" + "".join(f"{line}\n" for line in lines)
    return out


def insert_section(changelog: str, section: str) -> str:
    """``changelog`` with ``section`` above its newest release (one blank line between them)."""
    heading = section.split("\n", 1)[0]
    version = re.match(r"## \[([^\]]+)\]", heading)
    if version and re.search(rf"^## \[{re.escape(version.group(1))}\]", changelog, re.MULTILINE):
        raise ReleaseError(f"{CHANGELOG} already has a section for {version.group(1)}")
    first = re.search(r"^## \[", changelog, re.MULTILINE)
    if first is None:
        return changelog.rstrip("\n") + "\n\n" + section
    return changelog[: first.start()] + section + "\n" + changelog[first.start() :]


def section_for(changelog: str, version: str) -> str:
    """The CHANGELOG section of ``version`` (heading included), or "" when there is none."""
    match = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    return match.group(0).strip() + "\n" if match else ""


def pr_body(section: str) -> str:
    return (
        "Release pull request opened by `bin/release.py pr` after a merge into `main`; it replaces "
        "release-please. Once it is merged, `bin/release.py publish <PR#>` tags the merge commit and "
        "publishes the Forgejo release. Pushing to this branch is pointless: the next `bin/release.py pr` "
        "regenerates it.\n\n---\n\n" + section
    )


# --------------------------------------------------------------------------
# git and git-cliff
# --------------------------------------------------------------------------


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode:
        raise ReleaseError(f"git {' '.join(args[:3])} ... failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def cliff_context(tree: Path, revision_range: str, config: Path = ROOT / "cliff.toml") -> list[dict[str, Any]]:
    """git-cliff's parsed view of ``revision_range`` in the checkout at ``tree``."""
    uv = shutil.which("uv")
    if uv is None:
        raise ReleaseError("uv is not installed, so git-cliff cannot run")
    cmd = [uv, "tool", "run", "--from", GIT_CLIFF, "git-cliff", "--config", str(config), "--context", revision_range]
    try:
        proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, check=False, timeout=GIT_CLIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ReleaseError(f"git-cliff timed out after {GIT_CLIFF_TIMEOUT} s") from None
    if proc.returncode:
        raise ReleaseError(f"git-cliff failed (exit {proc.returncode}):\n{proc.stderr.strip()[-2000:]}")
    try:
        context = json.loads(proc.stdout)
    except ValueError as exc:
        raise ReleaseError("git-cliff --context printed something that is not JSON") from exc
    return [r for r in context if isinstance(r, dict)]


@contextlib.contextmanager
def worktree(sha: str) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="urbanlens-release-"))
    git("worktree", "add", "--detach", "--quiet", str(path), sha)
    try:
        yield path
    finally:
        git("worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        git("worktree", "prune", check=False)


def fetch_main_and_tags() -> str:
    """Fetch ``main`` (into a private ref) and the release tags from origin; return main's sha."""
    git("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{TARGET}:{PRIVATE_REF}")
    git("fetch", "--quiet", "--no-tags", "origin", "refs/tags/v*:refs/tags/v*")
    return git("rev-parse", PRIVATE_REF).strip()


# --------------------------------------------------------------------------
# The two steps
# --------------------------------------------------------------------------


def _release_pull(client: Any) -> dict[str, Any] | None:
    return next(
        (p for p in client.open_pulls(base=TARGET) if (p.get("head") or {}).get("ref") == RELEASE_BRANCH and (p.get("base") or {}).get("ref") == TARGET),
        None,
    )


def stale_problem(release_parent: str, base_tip: str) -> str | None:
    """
    None when the release commit sits directly on ``base_tip``, else why merging or tagging it is wrong.

    A release PR built on an older ``main`` (its ``pr`` step failed after a later merge) would
    tag commits its CHANGELOG section and version bump never counted, and the next release
    would count from that tag, so they would never be listed anywhere.
    """
    if release_parent == base_tip:
        return None
    return f"the release commit is built on {release_parent[:8]}, not on {TARGET}'s {base_tip[:8]}: it is stale; run bin/release.py pr to regenerate it"


def prepare_release_pr(client: Any, *, dry_run: bool = False, today: dt.date | None = None) -> int | None:
    """
    Open or update the release PR for what ``main`` holds since its last release; return its number.

    None when there is nothing to release (or on a dry run).
    """
    main_sha = fetch_main_and_tags()
    with worktree(main_sha) as tree:
        current = recorded_version(MANIFEST, (tree / MANIFEST).read_text(encoding="utf-8"))
        tag = f"v{current}"
        if not git("rev-parse", "--quiet", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).strip():
            raise ReleaseError(f"main records {current}, but there is no tag {tag}: publish it first (bin/release.py publish <release PR#>)")
        if not _is_ancestor(tag, main_sha):
            raise ReleaseError(f"{tag} is not on main")
        entries = entries_from_context(cliff_context(tree, f"{tag}..{main_sha}"))
        asked = release_as(git("log", "--format=%B", f"{tag}..{main_sha}"), current)
        if not entries and asked is None:
            print(f"release: nothing to release on main since {tag} (only changes the changelog leaves out)")
            return None
        version = asked or next_version(current, minor=bumps_minor(entries))
        section = render_section(version, current, today or dt.datetime.now(dt.UTC).date(), entries)
        changelog = tree / CHANGELOG
        changelog.write_text(insert_section(changelog.read_text(encoding="utf-8"), section), encoding="utf-8")
        for path in VERSION_FILES:
            target = tree / path
            target.write_text(bump_text(path, target.read_text(encoding="utf-8"), version), encoding="utf-8")
        title = TITLE.format(version=version)
        if dry_run:
            print(f"release: would open {title!r} ({len(entries)} entries):\n\n{section}")
            return None
        git("add", "--", CHANGELOG, *VERSION_FILES, cwd=tree)
        git("commit", "--quiet", "--no-verify", "-m", title, cwd=tree)
        new_tree = git("rev-parse", "HEAD^{tree}", cwd=tree).strip()
        remote = git("ls-remote", "origin", f"refs/heads/{RELEASE_BRANCH}").split()
        remote_sha = remote[0] if remote else ""
        if remote_sha and _same_release(remote_sha, main_sha, new_tree):
            print(f"release: {RELEASE_BRANCH} already holds {title}")
        else:
            git("push", "--quiet", "--no-verify", f"--force-with-lease=refs/heads/{RELEASE_BRANCH}:{remote_sha}", "origin", f"HEAD:refs/heads/{RELEASE_BRANCH}", cwd=tree)
    body = pr_body(section)
    pull = _release_pull(client)
    if pull is None:
        pull = client.create_pull(head=RELEASE_BRANCH, base=TARGET, title=title, body=body)
        print(f"release: opened #{pull['number']} {title}: {pull.get('html_url', '')}")
    elif pull.get("title") != title or pull.get("body") != body:
        client.edit_pull(pull["number"], title=title, body=body)
        print(f"release: updated #{pull['number']} to {title}: {pull.get('html_url', '')}")
    else:
        print(f"release: #{pull['number']} {title} is up to date: {pull.get('html_url', '')}")
    return int(pull["number"])


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=ROOT, check=False).returncode == 0


def _same_release(remote_sha: str, main_sha: str, new_tree: str) -> bool:
    """Whether the release branch already is main plus exactly this release commit."""
    ref = "refs/release-tool/release"
    git("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{RELEASE_BRANCH}:{ref}", check=False)
    if git("rev-parse", "--quiet", "--verify", ref, check=False).strip() != remote_sha:
        return False
    parent = git("rev-parse", "--quiet", "--verify", f"{ref}^", check=False).strip()
    tree = git("rev-parse", "--quiet", "--verify", f"{ref}^{{tree}}", check=False).strip()
    return parent == main_sha and tree == new_tree


def publish_release(client: Any, number: int) -> str:
    """Tag merged release PR ``number``'s merge commit and publish its Forgejo release; return the tag."""
    pull = client.pull(number)
    head, base = (pull.get("head") or {}).get("ref"), (pull.get("base") or {}).get("ref")
    match = TITLE_RE.match(pull.get("title") or "")
    if not pull.get("merged") or head != RELEASE_BRANCH or base != TARGET or not match:
        raise ReleaseError(f"#{number} is not a merged release PR ({RELEASE_BRANCH} into {TARGET}, titled {TITLE.format(version='X.Y.Z')!r})")
    version, merge_sha = match.group(1), str(pull.get("merge_commit_sha") or "")
    if parse_version(version)[0] != 0:
        raise ReleaseError(f"#{number} proposes {version}; UrbanLens stays 0.x until the maintainer decides otherwise")
    main_sha = fetch_main_and_tags()
    if not forgejo_api.SHA_RE.match(merge_sha) or not _is_ancestor(merge_sha, main_sha):
        raise ReleaseError(f"#{number}'s merge commit {merge_sha[:8] or '(none)'} is not on {TARGET}")
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    git("fetch", "--quiet", "--no-tags", "origin", f"+refs/pull/{int(number)}/head:refs/release-tool/pr")
    if git("rev-parse", "refs/release-tool/pr").strip() != head_sha:
        raise ReleaseError(f"#{number}'s head on origin is not {head_sha[:8]}")
    problem = stale_problem(git("rev-parse", f"{head_sha}^").strip(), git("rev-parse", f"{merge_sha}^1").strip())
    if problem:
        raise ReleaseError(f"#{number} was merged but not tagged: {problem}")
    for path in VERSION_FILES:
        recorded = recorded_version(path, git("show", f"{merge_sha}:{path}"))
        if recorded != version:
            raise ReleaseError(f"{path} at {merge_sha[:8]} records {recorded}, not {version}")
    notes = section_for(git("show", f"{merge_sha}:{CHANGELOG}"), version)
    if not notes:
        raise ReleaseError(f"{CHANGELOG} at {merge_sha[:8]} has no section for {version}")
    tag = f"v{version}"
    tagged = client.tag_sha(tag)
    if tagged and tagged != merge_sha:
        raise ReleaseError(f"{tag} already exists on {tagged[:8]}, not on #{number}'s merge commit {merge_sha[:8]}")
    if client.release_by_tag(tag):
        print(f"release: {tag} is already published")
    else:
        release = client.create_release(tag=tag, target=merge_sha, name=tag, body=notes, prerelease=PRERELEASE)
        print(f"release: published {tag} on {merge_sha[:8]}: {release.get('html_url', '')}")
    if client.tag_sha(tag) != merge_sha:
        raise ReleaseError(f"{tag} did not land on {merge_sha[:8]}")
    git("fetch", "--quiet", "--no-tags", "origin", f"refs/tags/{tag}:refs/tags/{tag}", check=False)
    return tag


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("pr", help="open or update the release PR for main")
    pr.add_argument("--dry-run", action="store_true", help="print the version and section; push and open nothing")
    publish = sub.add_parser("publish", help="tag a merged release PR and publish its Forgejo release")
    publish.add_argument("number", type=int)
    args = parser.parse_args(argv)
    client = forgejo_api.Forgejo()
    try:
        if args.command == "pr":
            prepare_release_pr(client, dry_run=args.dry_run)
        else:
            publish_release(client, args.number)
    except (ReleaseError, forgejo_api.ForgejoError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
