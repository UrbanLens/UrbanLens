#!/usr/bin/env python3
"""Create, list and destroy ephemeral dev environments.

    python3 bin/dev_env.py create --owner "agent: floorplans"
    python3 bin/dev_env.py list
    python3 bin/dev_env.py destroy a1b2c3

Each environment is a full UrbanLens + REData stack with its own checkout,
containers, database and hostname, created in about the time a build takes and
removed in seconds. Replaces the three fixed slots (s1/s2/s3), which ran out
and gave no way to tell which were free.

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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments, or None for ``sys.argv``.

    Returns:
        0 on success, 1 when a step failed.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Allocate and start a new environment.")
    create.add_argument("--name", default="", help="Preferred slug; one is generated when omitted.")
    create.add_argument("--branch", default="main", help="Branch to check out.")
    create.add_argument("--owner", default="", help="Who or what this is for, shown by `list`.")
    create.add_argument("--no-redata", action="store_true", help="Skip the per-environment REData instance.")

    sub.add_parser("list", help="Show allocated environments and whether they are running.")

    destroy = sub.add_parser("destroy", help="Stop an environment and release its name and ports.")
    destroy.add_argument("slug", help="Environment to remove.")
    destroy.add_argument("--keep-files", action="store_true", help="Leave the checkout on disk.")

    args = parser.parse_args(argv)

    if args.command == "create":
        record = devenv.create(requested_name=args.name, branch=args.branch, owner=args.owner, with_redata=not args.no_redata)
        print(json.dumps(record, indent=2))
        if record["status"] == "ok":
            print(f"\nReady: {record['context']['env']['url']}", file=sys.stderr)
        return 0 if record["status"] == "ok" else 1

    if args.command == "list":
        print(json.dumps(devenv.list_envs(), indent=2))
        return 0

    print(json.dumps(devenv.destroy(args.slug, keep_files=args.keep_files), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
