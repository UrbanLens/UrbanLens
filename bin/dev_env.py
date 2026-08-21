#!/usr/bin/env python3
"""Create, list and destroy ephemeral dev environments.

    python3 bin/dev_env.py create --owner "agent: floorplans"
    python3 bin/dev_env.py list
    python3 bin/dev_env.py destroy a1b2c3

Each environment is a full UrbanLens stack with its own checkout, containers,
database and hostname, created in about the time a build takes and removed in
seconds. Replaces the three fixed slots (s1/s2/s3), which ran out and gave no
way to tell which were free.

REData is shared with production by default (``--own-redata`` and
``--no-redata`` are the alternatives); ``opslib.devenv.REDATA_MODES`` explains
why a per-environment instance spends third-party quota rather than only disk.

Prints JSON, and exits non-zero when a step failed, so a caller can branch on
the exit code and read the detail only when it needs to.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opslib import devenv


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser.

    Split out from :func:`main` so the flag-to-mode mapping can be tested
    without running a create - the flags decide whether an environment spends
    external API quota, which is not something to find out by trying it.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Allocate and start a new environment.")
    create.add_argument("--name", default="", help="Preferred slug; one is generated when omitted.")
    create.add_argument("--branch", default="main", help="Branch to check out.")
    create.add_argument("--owner", default="", help="Who or what this is for, shown by `list`.")
    # Mutually exclusive because they are one decision: asking for a private
    # instance and for no REData at all is not a state anything can honour.
    redata_flags = create.add_mutually_exclusive_group()
    redata_flags.add_argument("--own-redata", action="store_true", help="Build a private REData for this environment (for work on the REData integration itself; ~8 minutes and eight extra containers).")
    redata_flags.add_argument("--no-redata", action="store_true", help="No REData at all; UL_REDATA_API_URL is left empty.")

    sub.add_parser("list", help="Show allocated environments and whether they are running.")

    destroy = sub.add_parser("destroy", help="Stop an environment and release its name and ports.")
    destroy.add_argument("slug", help="Environment to remove.")
    destroy.add_argument("--keep-files", action="store_true", help="Leave the checkout on disk.")
    return parser


def redata_mode(args: argparse.Namespace) -> str:
    """Which of ``devenv.REDATA_MODES`` the parsed flags ask for.

    Production is the default because a throwaway REData does not just cost
    eight containers: most REData "writes" make it fetch from external services,
    so a private instance burns third-party quota on data that is deleted with
    the environment.

    Args:
        args: Parsed ``create`` arguments.

    Returns:
        The mode.
    """
    if args.own_redata:
        return devenv.REDATA_OWN
    if args.no_redata:
        return devenv.REDATA_NONE
    return devenv.REDATA_PRODUCTION


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments, or None for ``sys.argv``.

    Returns:
        0 on success, 1 when a step failed.
    """
    args = build_parser().parse_args(argv)

    if args.command == "create":
        record = devenv.create(requested_name=args.name, branch=args.branch, owner=args.owner, redata=redata_mode(args))
        print(json.dumps(record, indent=2))
        if record["status"] == "ok":
            created = record["context"]["env"]
            print(f"\nReady: {created['url']} (REData: {created.get('redata_mode', devenv.REDATA_PRODUCTION)})", file=sys.stderr)
            # The credentials are in the JSON too; repeated here because the
            # point of seeding an account is that nobody has to go looking for
            # it. Empty when the seeding step could not run - which the step
            # record explains, and which is not a reason to claim a login.
            if created.get("login_username"):
                print(f"Log in as: {created['login_username']} / {created['login_password']}", file=sys.stderr)
        return 0 if record["status"] == "ok" else 1

    if args.command == "list":
        print(json.dumps(devenv.list_envs(), indent=2))
        return 0

    record = devenv.destroy(args.slug, keep_files=args.keep_files)
    print(json.dumps(record, indent=2))
    # A teardown that left containers running is not a successful destroy, and
    # this returned 0 for it - so a caller scripting `create`/`destroy` cycles
    # had no way to notice. `errors` carries the compose output that explains it.
    if record.get("errors"):
        print(f"\nTeardown incomplete: {'; '.join(record['errors'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
