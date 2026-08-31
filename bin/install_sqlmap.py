#!/usr/bin/env python3
"""Install a pinned, hash-verified sqlmap into an isolated virtual environment.

sqlmap (https://github.com/sqlmapproject/sqlmap) is not a dependency of the
application or of the main test suite - it is an external scanner invoked by
``bin/run_sqlmap_scan.sh``, the same relationship nuclei has via
``bin/run_nuclei_scan.sh``. It therefore does not belong in the project's own
``pyproject.toml``, which every contributor installs just to run ``ruff`` or
``pytest``; it gets its own throwaway environment instead, built on demand and
never on PATH.

The version and both PyPI-published SHA256 digests are pinned in
``bin/sqlmap-requirements.txt`` rather than here, so bumping the pin is a
one-file diff. ``pip install --require-hashes`` does the actual verification -
this script does not re-implement hash checking, it only ensures pip is asked
to do it and fails loudly if pip refuses.

Usage:
    python bin/install_sqlmap.py              # install if missing, print the path
    python bin/install_sqlmap.py --force      # reinstall even if already present
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".sqlmap" / "venv"
REQUIREMENTS = REPO_ROOT / "bin" / "sqlmap-requirements.txt"


def _venv_python(venv_dir: Path) -> Path:
    """Return the interpreter inside *venv_dir*, before or after creation."""
    return venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def _venv_sqlmap(venv_dir: Path) -> Path:
    """Return the ``sqlmap`` console-script entry point inside *venv_dir*."""
    return venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("sqlmap.exe" if os.name == "nt" else "sqlmap")


def _sqlmap_works(venv_dir: Path) -> bool:
    """Return True if the installed sqlmap can actually start.

    Probes with ``-h`` rather than ``--version``: the pip-packaged ``--version``
    prints its banner and then waits on a "Press Enter to continue..." prompt
    that only resolves itself when stdin is already closed (as it is in this
    check) - on a real terminal it would hang forever. ``-h`` prints and exits
    without asking anything, at every version tested.
    """
    exe = _venv_sqlmap(venv_dir)
    if not exe.is_file():
        return False
    try:
        result = subprocess.run([str(exe), "-h"], capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_installed(*, force: bool = False) -> Path:
    """Create the isolated venv (if needed) and hash-verify-install sqlmap into it.

    Args:
        force: Reinstall even if a working sqlmap is already present.

    Returns:
        Path to the ``sqlmap`` executable.

    Raises:
        RuntimeError: pip's hash verification failed, or the installed
            executable does not start.
    """
    if not force and _sqlmap_works(VENV_DIR):
        print(f"sqlmap already installed at {_venv_sqlmap(VENV_DIR)}", file=sys.stderr)
        return _venv_sqlmap(VENV_DIR)

    if force and VENV_DIR.exists():
        print(f"Removing existing environment at {VENV_DIR}", file=sys.stderr)
        shutil.rmtree(VENV_DIR)

    if not _venv_python(VENV_DIR).is_file():
        print(f"Creating isolated virtual environment at {VENV_DIR}", file=sys.stderr)
        # with_pip=True: --require-hashes below needs pip itself, and this
        # venv is never meant to see the project's own dependencies.
        venv.EnvBuilder(with_pip=True, clear=True).create(VENV_DIR)

    python = _venv_python(VENV_DIR)
    print(f"Installing sqlmap from {REQUIREMENTS} with --require-hashes", file=sys.stderr)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--require-hashes", "-r", str(REQUIREMENTS)],
        check=True,
        timeout=600,
        stdout=sys.stderr,
    )

    if not _sqlmap_works(VENV_DIR):
        raise RuntimeError(f"Installed sqlmap into {VENV_DIR} but `sqlmap -h` failed - the install is broken.")

    exe = _venv_sqlmap(VENV_DIR)
    print(f"sqlmap: {exe}", file=sys.stderr)
    return exe


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments.

    Args:
        argv: Argument list without the program name. ``None`` uses ``sys.argv``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Reinstall even if sqlmap is already present")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Install sqlmap and print where it landed.

    Every informational message goes to stderr; stdout carries only the final
    executable path, so ``bin/run_sqlmap_scan.sh`` can capture it directly with
    ``SQLMAP_BIN="$(python bin/install_sqlmap.py)"``.

    Args:
        argv: Argument list without the program name.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    if not REQUIREMENTS.is_file():
        print(f"error: {REQUIREMENTS} is missing.", file=sys.stderr)
        return 2
    try:
        exe = ensure_installed(force=args.force)
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(str(exe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
