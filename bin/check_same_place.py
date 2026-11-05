#!/usr/bin/env python3
"""Fail when code decides two pins are the same place without ``services.pins.same_place``."""

from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Attributes that name a pin's Location. Comparing two of them is an identity decision.
LOCATION_ATTRS = frozenset({"location", "location_id"})

#: Calls whose keywords are query filters. Constructors, ``create`` and ``update`` assign; they do not decide sameness.
FILTER_CALLS = frozenset({"filter", "exclude", "get", "Q", "Exists", "When"})

#: Receivers that are not named for a model; their keywords can be about anything, so they are always checked.
BARE_CALLS = frozenset({"Q", "Exists", "When"})

#: A method call on a receiver is a pin or wiki comparison when the receiver's text says so (``Pin.objects``, ``pins``, ``wikis``).
#: Lookups on other models (caches, images, slugs) hang off a Location and decide nothing about sameness.
RECEIVER_HINTS = ("pin", "wiki")

#: What may follow ``location`` / ``location_id`` in a filter key for it to be an identity, membership or radius test.
IDENTITY_SUFFIXES = (
    "",
    "_id",
    "__exact",
    "_id__exact",
    "__in",
    "_id__in",
    "__pk",
    "__pk__in",
    "__id",
    "__id__in",
    "__place",
    "__place__in",
    "__place__exact",
    "__place__id",
    "__place__id__in",
    "__place_id",
    "__place_id__in",
    "__place_id__exact",
    "__dwithin",
    "__point__dwithin",
    "__point__distance_lt",
    "__point__distance_lte",
)

#: ``values()`` arguments that pull Locations or Places out of a pin set.
LOCATION_COLUMNS = frozenset({"location", "location_id", "location__place_id", "location__place"})

#: The module that owns the rule.
OWNER = "src/urbanlens/dashboard/services/pins/same_place.py"

#: A trailing comment waives one decision: ``# same-place: <why the exact Location or a radius is meant>``.
WAIVER = "same-place:"

#: Modules that filter pins by radius. Each is a proximity search, not an identity decision.
#: Add to this only for another proximity search; "are these the same place" belongs in ``same_place``.
RADIUS_FILES = {
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


def _is_literal(value: ast.expr) -> bool:
    """Whether *value* is a constant, or a collection of them."""
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(element) for element in value.elts)
    return False


#: Where a key names the Location: at its start, or after a leading f-string placeholder (``f"{prefix}__location"``).
#: Relation paths such as ``wiki__location__in`` filter another model and are not checked.
LOCATION_START = re.compile(r"^(?:\0(?:__)?)?location")


def _is_identity_key(key: str) -> bool:
    r"""
    Whether a filter key tests a row's Location (or its Place or point) against something.

    The key may start with an f-string placeholder (``\0`` here).
    A dynamic tail counts as identity unless its literal text already rules out every identity suffix (``location__official_name__\\0``).
    """
    for match in LOCATION_START.finditer(key):
        tail = key[match.end() :]
        if "\0" in tail:
            literal = tail.split("\0", 1)[0]
            if any(suffix.startswith(literal) for suffix in IDENTITY_SUFFIXES):
                return True
        elif tail in IDENTITY_SUFFIXES:
            return True
    return False


def _values_of_location(node: ast.AST) -> bool:
    """Whether *node* contains ``values``/``values_list`` of a Location column."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in {"values", "values_list"}:
            if any(isinstance(arg, ast.Constant) and arg.value in LOCATION_COLUMNS for arg in child.args):
                return True
    return False


def _about_pins_or_wikis(call: ast.Call, name: str) -> bool:
    """Whether *call* filters pins or wikis, judged by its receiver; bare ``Q``/``Exists`` always count."""
    if name in BARE_CALLS:
        return True
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = ast.unparse(call.func.value).lower()
    return any(hint in receiver for hint in RECEIVER_HINTS)


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _filter_keys(call: ast.Call) -> list[tuple[str, ast.expr]]:
    """Return (key, value) for each keyword of *call*, including literal keys of a ``**{...}`` argument."""
    keys: list[tuple[str, ast.expr]] = []
    for keyword in call.keywords:
        if keyword.arg is not None:
            keys.append((keyword.arg, keyword.value))
        elif isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=False):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append((key.value, value))
                elif isinstance(key, ast.JoinedStr):
                    # An f-string key keeps its literal text with each placeholder as "\0"; _is_identity_key treats an unknown suffix as an identity test.
                    keys.append(("".join(str(v.value) if isinstance(v, ast.Constant) else "\0" for v in key.values), value))
    return keys


def _waived(lines: list[str], first: int, last: int) -> bool:
    """Whether a waiver sits on lines *first*..*last* (1-based) or in a comment-only line above."""
    own = lines[first - 1 : last]
    above = lines[first - 2] if first >= 2 else ""
    return any(WAIVER in line for line in own) or (above.lstrip().startswith("#") and WAIVER in above)


def _location_sets(tree: ast.AST) -> set[str]:
    """Names assigned a set of Location ids taken from a pin queryset."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _values_of_location(node.value):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _is_location_set(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Starred):
        node = node.value
    return _values_of_location(node) or (isinstance(node, ast.Name) and node.id in names)


def find_violations(source: str, rel_path: str) -> list[str]:
    """
    Describe each same-place decision in *source* that bypasses ``same_place``.

    Args:
        source: Python source text.
        rel_path: Repo-relative path, matched against the radius allowlist.

    Returns:
        One ``path:line: reason`` string per violation.
    """
    if rel_path == OWNER:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    names = _location_sets(tree)
    found: list[str] = []
    simple = [(n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree) if isinstance(n, ast.stmt) and not hasattr(n, "body")]
    simple += [(n.lineno, n.body[0].lineno - 1) for n in ast.walk(tree) if isinstance(n, (ast.If, ast.While)) and n.body]

    def report(node: ast.AST, reason: str) -> None:
        first = getattr(node, "lineno", 1)
        last = getattr(node, "end_lineno", first) or first
        # A formatter may wrap the call, so the waiver can sit anywhere in the enclosing statement.
        around = [span for span in simple if span[0] <= first and last <= span[1]]
        if around:
            first, last = min(around, key=lambda span: span[1] - span[0])
        if not _waived(lines, first, last):
            found.append(f"{rel_path}:{getattr(node, 'lineno', first)}: {reason}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in FILTER_CALLS and _about_pins_or_wikis(node, name):
                for key, value in _filter_keys(node):
                    if not _is_identity_key(key) or _is_literal(value):
                        continue
                    if "dwithin" in key or "distance_lt" in key:
                        if rel_path not in RADIUS_FILES:
                            report(value, f"{key} matches pins by radius")
                    else:
                        report(value, f"{key} matches rows by Location outside same_place")
            elif name in {"intersection", "intersection_update"} and any(_is_location_set(arg, names) for arg in [*node.args, node.func.value if isinstance(node.func, ast.Attribute) else node]):
                report(node, "Location ids of pin sets intersected")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd) and _is_location_set(node.left, names) and _is_location_set(node.right, names):
            report(node, "Location ids of pin sets intersected")
        elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops):
            operands = [node.left, *node.comparators]
            attrs = sum(isinstance(o, ast.Attribute) and o.attr in LOCATION_ATTRS for o in operands)
            member_of_set = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and attrs >= 1 and any(_is_location_set(o, names) for o in node.comparators)
            if attrs >= 2 or member_of_set:
                report(node, "two locations compared for identity")
    return found


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
        sys.stderr.write("\nUse same_place() / same_place_q(). Waive an intended exact-Location match with a trailing '# same-place: <why>'; a proximity-search module goes in RADIUS_FILES.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
