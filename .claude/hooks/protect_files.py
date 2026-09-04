#!/usr/bin/env python3
"""PreToolUse guard: stop an agent editing files it should not edit.

Registered in .claude/settings.json for Edit|Write|NotebookEdit|Bash. Claude
Code pipes the tool call in as JSON on stdin; exit 2 blocks the call and shows
the agent whatever this prints to stderr. Exit 0 means "no opinion" and the
normal permission flow continues.

It watches Bash too, not only the edit tools: `sed -i ... CLAUDE.md` and
`echo ... > CLAUDE.md` would otherwise walk straight past a guard that only
matched Edit and Write. Reading a protected file is always fine -- only writes
are blocked -- so `grep x CLAUDE.md > out.txt` passes and `sed -i s/a/b/
CLAUDE.md` does not.

To edit a protected file deliberately, launch Claude with the override set:

    CLAUDE_ALLOW_PROTECTED_EDITS=1 claude

That is a human decision made outside the session, which is the point.
"""

import json
import os
import re
import sys

# path fragment -> what to do instead
PROTECTED = {
    "CLAUDE.md": (
        "CLAUDE.md is read by every session and every subagent, so every line "
        "costs tokens on every task. It is kept under 140 lines deliberately.\n"
        "Write what you learned in docs/ instead:\n"
        "  - a defect            -> docs/problems.md      (next P id in docs/INDEX.md)\n"
        "  - a measurement       -> docs/experiments.md   (X id, and state its unit)\n"
        "  - a choice you made   -> docs/decisions.md     (D id)\n"
        "  - work to do          -> docs/tasks.md         (T id)\n"
        "  - how something works -> docs/reference/       (R id)\n"
        "and add the one-line entry to docs/INDEX.md in the same commit.\n"
        "If it truly belongs in CLAUDE.md, print the exact replacement lines in "
        "your reply and let the user apply them."
    ),
    "docs/GOALS.md": ("GOALS.md is Jess' notes on project goals, written in their voice. Do not edit, summarise, or 'tidy' it. If you have something to say about it, write your own docs/ entry and cite SRC."),
}

# Commands that modify a file named as one of their arguments.
_MUTATORS = r"sed\s+-[a-zA-Z]*i|tee|truncate|shred|dd\b|rm\b|mv\b|cp\b|install\b|patch\b"

# `cmd <<'EOF' ... EOF` / `<<-EOF` / `<<EOF`, non-greedy to the closing word.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _executable_text(command: str) -> str:
    """`command` with heredoc bodies and quoted strings removed.

    Text inside a heredoc or a quoted string is DATA, not a command. Without
    this, writing a commit message that mentions `sed -i ... CLAUDE.md` -- or a
    doc explaining what this hook blocks -- gets blocked by the hook itself.
    That happened while this file was being written.

    Stripping quotes keeps the real cases: `sed -i 's/a/b/' CLAUDE.md` still
    shows `sed -i` and `CLAUDE.md` once the script argument is gone.
    """
    return _QUOTED.sub(" ", _HEREDOC.sub(" ", command))


def _bash_writes_to(command: str, fragment: str) -> bool:
    """True if `command` looks like it WRITES to a path containing `fragment`.

    Reading is always allowed, so this must not fire on `cat`, `grep`, `head`
    or a redirect whose target is some other file. It also must not fire on
    `git add`/`git commit`/`git checkout`, which legitimately touch any file
    and are not an agent hand-editing it.
    """
    text = _executable_text(command)
    if not _command_mentions(text, fragment):
        return False
    escaped = re.escape(fragment)
    # `> path` or `>> path` where the redirect TARGET contains the fragment
    if re.search(r">>?\s*[^\s|;&<>]*" + escaped, text):
        return True
    # a mutating command with the path among its arguments, within one segment
    return bool(re.search(r"\b(" + _MUTATORS + r")[^|;&\n]*" + escaped, text))


# A filename character. Used to require that a protected name matches a WHOLE
# path component: without this, `.CLAUDE.md.swp` (vim's swap file) and
# `CLAUDE.md.orig` (a merge leftover) both contain "CLAUDE.md" as a substring
# and were blocked, which is wrong -- they are different files. That happened.
_NAME_CHAR = r"[A-Za-z0-9._-]"


def _path_matches(path: str, fragment: str) -> bool:
    """Does `path` actually refer to the protected `fragment`?

    A fragment ending in `/` is a directory prefix. Anything else must match a
    complete path suffix, so `CLAUDE.md` matches `./CLAUDE.md` and
    `/a/b/CLAUDE.md` but not `.CLAUDE.md.swp`.
    """
    if fragment.endswith("/"):
        return fragment in path
    return path == fragment or path.endswith("/" + fragment)


def _command_mentions(command: str, fragment: str) -> bool:
    """As above, for a shell command: the fragment must not be glued to more
    filename characters on either side."""
    pattern = f"(?<!{_NAME_CHAR}){re.escape(fragment)}(?!{_NAME_CHAR})"
    return re.search(pattern, command) is not None


def _blocked_path(payload: dict) -> tuple:
    """Return (matched_fragment, offending_text) or (None, None)."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        command = tool_input.get("command") or ""
        for fragment in PROTECTED:
            if _command_mentions(command, fragment) and _bash_writes_to(command, fragment):
                return fragment, command
        return None, None

    path = (tool_input.get("file_path") or "").replace("\\", "/")
    if path:
        for fragment in PROTECTED:
            if _path_matches(path, fragment):
                return fragment, path
    return None, None


def main() -> int:
    if os.environ.get("CLAUDE_ALLOW_PROTECTED_EDITS") == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed input is not a reason to block real work.
        return 0

    fragment, offending = _blocked_path(payload)
    if fragment is None:
        return 0

    print(
        f"Blocked: this would write to {fragment}, which is protected by .claude/hooks/protect_files.py\n  {offending[:200]}",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(PROTECTED[fragment], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
