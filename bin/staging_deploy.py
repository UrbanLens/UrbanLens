#!/usr/bin/env python3
"""Deploy staging from a branch and verify it actually works.

Thin wrapper so the pipeline can be run from a checkout without installing
anything: ``python3 bin/staging_deploy.py --branch main --prod-dir ...``.

Exits 0 only when every step passed, and prints the run record as JSON, so a
caller can branch on the exit code and read the detail only when it matters.
See ``bin/opslib/staging.py`` for the steps and why they are ordered as they
are.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opslib.staging import main

if __name__ == "__main__":
    raise SystemExit(main())
