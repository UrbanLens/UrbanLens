#!/usr/bin/env python3
"""Fail when code decides two pins are the same place without ``services.pins.same_place``."""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Attributes that name a pin's Location. Comparing two of them is an identity decision.
LOCATION_ATTRS = frozenset({"location", "location_id"})

#: Keywords that filter rows by Location membership.
LOCATION_KEYWORDS = frozenset({"location", "location_id", "location__in", "location_id__in", "location__place_id__in", "location__place__in"})

#: ``values()`` arguments that pull another pin set's Locations or Places into such a filter.
LOCATION_COLUMNS = frozenset({"location", "location_id", "location__place_id", "location__place"})

#: The module that owns the rule.
OWNER = "src/urbanlens/dashboard/services/pins/same_place.py"

#: Files allowed to filter a pin's Location by radius. Each is a proximity search, not an identity decision.
#: Add to this only for another proximity search; "are these the same place" belongs in ``same_place``.
DWITHIN_ALLOWED = {
    "src/urbanlens/dashboard/models/pin/queryset.py": "map search near a point",
    "src/urbanlens/dashboard/models/pin_share/queryset.py": "share provenance: 150 m on purpose (ADR34 section 7)",
    "src/urbanlens/dashboard/services/sharing/share_provenance.py": "share provenance: 150 m on purpose (ADR34 section 7)",
    "src/urbanlens/dashboard/services/visits/visits.py": "nearest own pin to a GPS fix",
    "src/urbanlens/dashboard/services/memories/photos.py": "nearest own pin to a photo",
    "src/urbanlens/dashboard/services/memories/visits.py": "nearest own pin to a photo",
    "src/urbanlens/dashboard/services/import_formats/gpx_tracks.py": "pins along a recorded track",
    "src/urbanlens/dashboard/services/pins/public_pins.py": "public candidates in a region",
    "src/urbanlens/dashboard/services/device_scan/clustering.py": "device markers near a marker",
}

#: Files allowed to compare Locations directly: storage and hierarchy rules that mean the exact row
#: (``db_pin_unique_location_per_profile``, parent/child nesting), not "the same place" for a person.
IDENTITY_ALLOWED = {
    "src/urbanlens/dashboard/controllers/boundary.py": "keeps a boundary row's Location equal to its pin's",
    "src/urbanlens/dashboard/controllers/image_gallery.py": "a photo stamped with the pin's exact Location; already limited to photos the viewer can see",
    "src/urbanlens/dashboard/models/pin/model.py": "child/root hierarchy on the exact Location (ADR34 section 3)",
    "src/urbanlens/dashboard/services/pins/external_data.py": "site-root hierarchy on the exact Location",
    "src/urbanlens/dashboard/services/pins/pin_edit.py": "child/root hierarchy on the exact Location (ADR34 section 3)",
}


def find_violations(source: str, rel_path: str) -> list[str]:
    """
    Describe each same-place decision in *source* that bypasses ``same_place``.

    Args:
        source: Python source text.
        rel_path: Repo-relative path, matched against the allowlists.

    Returns:
        One ``path:line: reason`` string per violation.
    """
    if rel_path == OWNER:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg is not None:
            if "dwithin" in node.arg and node.arg.startswith("location__") and rel_path not in DWITHIN_ALLOWED:
                found.append(f"{rel_path}:{node.value.lineno}: {node.arg} matches pins by radius")
            if node.arg in LOCATION_KEYWORDS and rel_path not in IDENTITY_ALLOWED and _draws_from_another_pin_set(node.value):
                found.append(f"{rel_path}:{node.value.lineno}: {node.arg} matches against another pin set's locations")
        elif isinstance(node, ast.Compare) and rel_path not in IDENTITY_ALLOWED:
            operands = [node.left, *node.comparators]
            if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops) and sum(isinstance(o, ast.Attribute) and o.attr in LOCATION_ATTRS for o in operands) >= 2:
                found.append(f"{rel_path}:{node.lineno}: two locations compared for identity")
    return found


def _draws_from_another_pin_set(value: ast.expr) -> bool:
    """Whether *value* is built by ``values``/``values_list`` of a Location column."""
    for node in ast.walk(value):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"values", "values_list"}:
            if any(isinstance(arg, ast.Constant) and arg.value in LOCATION_COLUMNS for arg in node.args):
                return True
    return False


def scan_tree(root: pathlib.Path = SEARCH_ROOT) -> list[str]:
    """Return every violation under *root*, skipping tests and migrations."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or "/migrations/" in posix:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else posix
        problems.extend(find_violations(path.read_text(encoding="utf-8"), rel))
    return problems


def main() -> int:
    problems = scan_tree()
    if problems:
        sys.stderr.write("Same-place decisions made without services.pins.same_place:\n")
        for problem in problems:
            sys.stderr.write(f"  {problem}\n")
        sys.stderr.write("\nUse same_place() / same_place_q(). A proximity search belongs in DWITHIN_ALLOWED in bin/check_same_place.py.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
