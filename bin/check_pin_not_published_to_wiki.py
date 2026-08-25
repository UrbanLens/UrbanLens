#!/usr/bin/env python3
"""Fail if production code attaches a pin-owned row to a wiki in the same breath.

``services/photos/uploads.py`` built a photo's ownership like this::

    return {"pin": owner, "wiki": Wiki.objects.get_for_location(location), "location": location}

so every photo uploaded to somebody's own pin was also published to that place's
community wiki, where other people could see it and vote on its relevance. The
uploader never chose to contribute it. Their ``photo_upload_visibility`` narrowed
who saw the result, which is a control over the audience for things you have
shared - not consent to share.

Seven models carry both a ``pin`` and a ``wiki`` foreign key (Image, Comment,
Link, Alias, Boundary, Floorplan, AutoRemoval). For all of them the two columns
are alternatives: a row belongs to somebody's pin, *or* it is on the shared wiki.
Setting both at once is what publishes private content, and it is invisible in
review precisely because it reads as ordinary bookkeeping.

So this check does not care about ``wiki=`` on its own - that is how a genuine
wiki row is made, and there are over a hundred legitimate ones. It flags a single
construction that names *both*, which is the shape of the bug and almost never
the shape of anything else.

Sharing is a deliberate act with a place of its own:
``services.photos.attachment.attach_to_wiki`` records who chose it. A path that
must set both - a migration repairing old rows, a service that has just taken the
user's decision - marks itself, next to the code, with:

    # pin-to-wiki-ok: <why>

Tests, migrations and baker recipes are exempt: a test constructing an
inconsistent row is being specific on purpose, and several assert exactly that
this pairing does not happen by itself.

Exits non-zero listing each offending construction. Safe to run from the repo
root.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEARCH_ROOT = REPO_ROOT / "src" / "urbanlens"

#: Both spellings, because a dict of kwargs and a direct call look different.
_PIN_KEYS = frozenset({"pin", "pin_id"})
_WIKI_KEYS = frozenset({"wiki", "wiki_id"})

_ALLOW_MARKER = "pin-to-wiki-ok:"

#: Paths that may pair them. Tests are deliberate; migrations repair history;
#: baker recipes build fixtures.
_EXEMPT_PARTS = ("tests", "migrations", "baker_recipes.py", "conftest.py")


def _is_none(node: ast.expr) -> bool:
    """Whether a value is a literal None - clearing a column, not setting it."""
    return isinstance(node, ast.Constant) and node.value is None


def _offending_keys(keys: list[str | None], values: list[ast.expr]) -> bool:
    """Whether this construction names a pin and *derives* a wiki on the spot.

    Passing both along is not the bug - ``upload_photo(..., pin=pin, wiki=wiki)``
    is a signature whose callers supply what they mean, and usually one of them
    is None. The bug is working the wiki out from the pin's own location while
    building a pin-owned row, which is how a private upload ends up on a shared
    page without anybody deciding it should::

        {"pin": owner, "wiki": Wiki.objects.get_for_location(location), ...}

    So a wiki value that is a *call* alongside a pin is what gets flagged; a
    plain name, attribute or None is left alone.
    """
    named = {key: value for key, value in zip(keys, values, strict=False) if key is not None}
    pin = next((named[k] for k in _PIN_KEYS if k in named), None)
    wiki = next((named[k] for k in _WIKI_KEYS if k in named), None)
    if pin is None or wiki is None:
        return False
    if _is_none(pin) or _is_none(wiki):
        return False
    return isinstance(wiki, ast.Call)


class _Visitor(ast.NodeVisitor):
    """Collects constructions naming both a pin and a wiki."""

    def __init__(self, path: pathlib.Path, source: str) -> None:
        self.path = path
        self.lines = source.splitlines()
        self.offences: list[tuple[int, str]] = []

    def _allowed(self, lineno: int) -> bool:
        """Whether the marker appears on, just above, or just below the line."""
        window = self.lines[max(0, lineno - 3) : lineno + 2]
        return any(_ALLOW_MARKER in line for line in window)

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = [key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None for key in node.keys]
        if _offending_keys(keys, node.values) and not self._allowed(node.lineno):
            self.offences.append((node.lineno, "a dict naming both pin and wiki"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        keys = [kw.arg for kw in node.keywords]
        values = [kw.value for kw in node.keywords]
        if _offending_keys(keys, values) and not self._allowed(node.lineno):
            self.offences.append((node.lineno, "a call passing both pin and wiki"))
        self.generic_visit(node)


def main() -> int:
    """Scan production code and report constructions that publish a pin row to a wiki.

    Returns:
        0 when clean, 1 when something pairs them without a marker.
    """
    offences: list[str] = []
    for path in sorted(SEARCH_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if any(part in _EXEMPT_PARTS for part in relative.parts) or relative.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        if "wiki" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover - a broken file is someone else's failure
            offences.append(f"{relative}: could not parse ({exc})")
            continue
        visitor = _Visitor(path, source)
        visitor.visit(tree)
        offences.extend(f"{relative}:{line}: {what}" for line, what in visitor.offences)

    if offences:
        print(f"Pin-owned rows published to a wiki ({len(offences)}):")
        for offence in offences:
            print(f"  {offence}")
        print()
        print("A pin row and a wiki row are alternatives. Sharing is deliberate:")
        print("  services.photos.attachment.attach_to_wiki(image, wiki, added_by=profile)")
        print(f"If a path genuinely needs both, mark it: # {_ALLOW_MARKER} <why>")
        return 1

    print("No production code attaches a pin-owned row to a wiki.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
