#!/usr/bin/env python3
"""PostToolUse hook: format and auto-fix any Python file Claude just wrote.

Registered in .claude/settings.json for Edit|Write|NotebookEdit. Formatting a
file the moment it is written keeps it off the commit path, so no session spends
a turn reasoning about import order or line width. It overlaps deliberately with
the `autofix` hook in .pre-commit-config.yaml, which is the backstop for files
this hook never saw - a hand edit, a rebase, a merge resolution.

Deliberately silent and deliberately harmless:
  - if ruff is not installed, it does nothing and says nothing;
  - it only ever applies ruff's SAFE fixes (no --unsafe-fixes), so it cannot
    change what the code does;
  - it never fails the tool call. A formatter that can block an edit is worse
    than no formatter.
"""

import json
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 20


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py") or not Path(path).is_file():
        return 0

    for args in (["check", "--fix", "--quiet"], ["format", "--quiet"]):
        try:
            subprocess.run(
                [sys.executable, "-m", "ruff", *args, path],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0  # ruff missing or wedged -- not this hook's problem
    return 0


if __name__ == "__main__":
    sys.exit(main())
