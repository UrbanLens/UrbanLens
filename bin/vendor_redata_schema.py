#!/usr/bin/env python3
"""Vendor the part of REData's OpenAPI document that UrbanLens reads, for the consumer contract test.

``tests/hypothesis/test_redata_consumer_contract.py`` holds every field UrbanLens reads from REData against REData's
published schema. The schema is vendored rather than fetched so the test runs offline; this script refreshes it,
keeping only the operations named in ``tests/hypothesis/redata_contract.py`` and the components they reference.

Get a full schema from a REData checkout (no server needed, GDAL must be importable)::

    cd ../REData/src/redata && uv run python manage.py spectacular --format openapi-json --file /tmp/redata.json

or from any deployment, which serves it without a key::

    curl -s 'https://redata.example/api/v1/schema/?format=json' > /tmp/redata.json

Then::

    uv run python bin/vendor_redata_schema.py /tmp/redata.json --revision "release/0.3.0 621e98c8"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from urbanlens.dashboard.tests.hypothesis.redata_contract import READS, VENDORED_SCHEMA

#: Keys that describe rather than constrain; dropping them keeps the vendored file reviewable.
_PROSE_KEYS = frozenset({"description", "summary", "example", "examples", "x-spec-enum-id"})
#: Keys whose values map names to schemas, where a prose-looking key is a field name.
_NAMED_KEYS = frozenset({"properties", "schemas", "responses", "content"})


def _without_prose(node: Any, *, named: bool = False) -> Any:
    """``node`` without its prose keys, recursively.

    Args:
        node: Part of an OpenAPI document.
        named: ``node`` maps names to schemas (``properties``, ``schemas``), so a key called ``description`` is a field.

    Returns:
        The same structure, minus descriptions and examples.
    """
    if isinstance(node, dict):
        return {key: _without_prose(value, named=key in _NAMED_KEYS and not named) for key, value in node.items() if named or key not in _PROSE_KEYS}
    if isinstance(node, list):
        return [_without_prose(value) for value in node]
    return node


def _refs(node: Any) -> set[str]:
    """The component names ``node`` references directly or through nesting.

    Args:
        node: Part of an OpenAPI document.

    Returns:
        Schema component names.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found


def trimmed(schema: dict[str, Any], revision: str) -> dict[str, Any]:
    """The operations the contract table names, their responses, and every component those reference.

    Args:
        schema: REData's full OpenAPI document.
        revision: Which REData the document came from, recorded in the output.

    Returns:
        The vendored document.

    Raises:
        SystemExit: The table names an operation REData does not publish.
    """
    paths: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for read in READS:
        operation = schema.get("paths", {}).get(read.path, {}).get(read.method)
        if operation is None:
            missing.append(f"{read.method.upper()} {read.path}")
            continue
        paths.setdefault(read.path, {})[read.method] = {"responses": operation.get("responses", {})}
    if missing:
        raise SystemExit("REData publishes no such operation: " + ", ".join(sorted(set(missing))))

    components = schema.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    pending = _refs(paths)
    while pending:
        name = pending.pop()
        if name in wanted or name not in components:
            continue
        wanted.add(name)
        pending |= _refs(components[name])

    return _without_prose(
        {
            "openapi": schema.get("openapi"),
            "info": {"title": schema.get("info", {}).get("title"), "version": schema.get("info", {}).get("version"), "x-redata-revision": revision},
            "paths": {path: paths[path] for path in sorted(paths)},
            "components": {"schemas": {name: components[name] for name in sorted(wanted)}},
        },
    )


def main() -> int:
    """Read a full schema, write the vendored one.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("schema", type=pathlib.Path, help="REData's full OpenAPI document, as JSON")
    parser.add_argument("--revision", required=True, help="which REData it came from, e.g. 'release/0.3.0 621e98c8'")
    args = parser.parse_args()

    document = trimmed(json.loads(args.schema.read_text()), args.revision)
    VENDORED_SCHEMA.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    print(f"wrote {VENDORED_SCHEMA.relative_to(REPO_ROOT)}: {len(document['paths'])} paths, {len(document['components']['schemas'])} components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
