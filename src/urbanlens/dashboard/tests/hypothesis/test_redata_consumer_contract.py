"""Every field UrbanLens reads from REData is one REData publishes, where UrbanLens reads it.

The table is ``redata_contract.READS``: one row per UrbanLens reader and REData operation, listing the response fields
the reader uses. It is checked against REData's own OpenAPI document, vendored by ``bin/vendor_redata_schema.py``.
A failure here is a wire mismatch - the kind that left the Historical Maps gallery empty because it read ``sheet``
fields at the top level - or a table that has fallen behind the code it describes.
"""

from __future__ import annotations

import json
from typing import Any

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.tests.hypothesis.redata_contract import READS, VENDORED_OVERTURE_SHARDS, VENDORED_SCHEMA, Read

_JSON = "application/json"


def _load() -> dict[str, Any]:
    return json.loads(VENDORED_SCHEMA.read_text())


class _Resolver:
    """Walks a field path through one response schema.

    Path syntax: dot-separated keys from the body root; ``key[]`` steps into each item of the array at ``key``; a
    leading ``[]`` steps into a body that is itself an array; ``{}`` steps into each value of a free-keyed map; a final
    ``*`` marks an object REData publishes untyped, which UrbanLens reads keys from without the schema's say-so.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self.components = schema["components"]["schemas"]

    def _deref(self, node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            node = self.components[node["$ref"].rsplit("/", 1)[1]]
        return node

    def _alternatives(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """Every schema ``node`` may be: itself, or any member of an ``allOf``/``oneOf``/``anyOf``, flattened."""
        node = self._deref(node)
        members = [*node.get("allOf", []), *node.get("oneOf", []), *node.get("anyOf", [])]
        if not members:
            return [node]
        found = [node] if node.get("properties") or node.get("items") or node.get("additionalProperties") else []
        for member in members:
            found.extend(self._alternatives(member))
        return found

    @staticmethod
    def _untyped(node: dict[str, Any]) -> bool:
        return not node.get("properties") and not node.get("items") and node.get("type") in (None, "object")

    def _step(self, nodes: list[dict[str, Any]], segment: str) -> list[dict[str, Any]]:
        key, _, rest = segment.partition("[]")
        found: list[dict[str, Any]] = []
        for node in nodes:
            for option in self._alternatives(node):
                if key == "{}":
                    extra = option.get("additionalProperties")
                    if isinstance(extra, dict):
                        found.append(extra)
                    continue
                target = option.get("properties", {}).get(key) if key else option
                if target is None:
                    continue
                if segment.endswith("[]"):
                    found.extend(alt["items"] for alt in self._alternatives(target) if "items" in alt)
                elif not rest:
                    found.append(target)
        return found

    def problem(self, body: dict[str, Any], path: str) -> str | None:
        """Why ``path`` does not resolve in ``body``, or None when it does.

        Args:
            body: The response schema.
            path: A field path in the syntax above.

        Returns:
            A one-line reason, or None.
        """
        nodes = [body]
        segments = path.split(".")
        for index, segment in enumerate(segments):
            if segment == "*":
                if index != len(segments) - 1:
                    return "'*' must end the path"
                if not any(self._untyped(option) for node in nodes for option in self._alternatives(node)):
                    return "REData now types this object; list the fields read instead of '*'"
                return None
            nodes = self._step(nodes, segment)
            if not nodes:
                return f"no {'.'.join(segments[: index + 1])!r}"
        return None


def _response(schema: dict[str, Any], read: Read) -> dict[str, Any] | None:
    return schema["paths"].get(read.path, {}).get(read.method, {}).get("responses", {}).get(read.status)


def _response_schema(schema: dict[str, Any], read: Read) -> dict[str, Any] | None:
    response = _response(schema, read)
    return None if response is None else response.get("content", {}).get(_JSON, {}).get("schema")


class ConsumerContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.schema = _load()
        cls.resolver = _Resolver(cls.schema)

    def test_every_field_read_is_published(self) -> None:
        problems = []
        for read in READS:
            where = f"{read.reader}: {read.method.upper()} {read.path} [{read.status}]"
            if _response(self.schema, read) is None:
                problems.append(f"{where}: REData documents no such response")
                continue
            if not read.fields:
                continue
            body = _response_schema(self.schema, read)
            if body is None:
                problems.append(f"{where}: REData documents no JSON body")
                continue
            problems.extend(
                f"{where} {field}: {reason}"
                for field in read.fields
                if (reason := self.resolver.problem(body, field)) is not None
            )

        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_gap_is_still_a_gap(self) -> None:
        """A gap REData has since filled is a field to move into ``fields``; one UrbanLens stopped reading, to drop."""
        filled = []
        for read in READS:
            body = _response_schema(self.schema, read) or {}
            filled.extend(
                f"{read.reader}: {read.method.upper()} {read.path} [{read.status}] {gap}"
                for gap in read.gaps
                if self.resolver.problem(body, gap) is None
            )

        self.assertEqual(filled, [], "\n".join(filled))

    def test_the_table_names_each_reader_and_operation_once(self) -> None:
        keys = [(read.reader, read.method, read.path, read.status) for read in READS]

        self.assertEqual(len(keys), len(set(keys)))

    def test_the_vendored_schema_holds_nothing_the_table_does_not_read(self) -> None:
        """Re-vendoring prunes to the table, so an operation left here is one the table dropped without re-vendoring."""
        read = {(read.path, read.method) for read in READS}
        vendored = {(path, method) for path, operations in self.schema["paths"].items() for method in operations}

        self.assertEqual(vendored - read, set())

    def test_the_resolver_rejects_a_misplaced_field(self) -> None:
        """Anti-vacuity: the gallery's old top-level read must fail, and its nested read must pass."""
        maps = next(read for read in READS if read.path == "/api/v1/maps/")
        body = _response_schema(self.schema, maps)
        assert body is not None

        self.assertIsNotNone(self.resolver.problem(body, "results[].thumbnail_url"))
        self.assertIsNone(self.resolver.problem(body, "results[].sheet.thumbnail_url"))


class IncidentVocabularyTests(SimpleTestCase):
    """REData's incident ``category`` is a closed vocabulary that UrbanLens both labels and sends back as a filter."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        resolver = _Resolver(_load())
        incident = resolver._deref({"$ref": "#/components/schemas/Incident"})
        category = resolver._deref(incident["properties"]["category"])
        cls.published = set(category["enum"])

    def test_every_category_redata_publishes_is_labelled(self) -> None:
        """A new category is one someone must classify as crime or not before it is counted."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import INCIDENT_CATEGORY_LABELS

        self.assertEqual(self.published - set(INCIDENT_CATEGORY_LABELS), set())

    def test_every_crime_category_sent_as_a_filter_exists(self) -> None:
        """REData answers 400 for an unknown ``category``, which would hide both incident panels."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import CRIME_INCIDENT_CATEGORIES

        self.assertEqual(set(CRIME_INCIDENT_CATEGORIES) - self.published, set())


class OvertureShardTableTests(SimpleTestCase):
    """REData syncs Overture only inside its state shard boxes, and UrbanLens decides who answers by the same boxes."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.vendored = json.loads(VENDORED_OVERTURE_SHARDS.read_text())

    def test_the_routing_table_is_redatas(self) -> None:
        """A box REData widens or adds sends UrbanLens's reads there to REData only once this table says so."""
        from urbanlens.dashboard.services.apis.locations.boundaries.redata_overture_shards import (
            REDATA_OVERTURE_SHARD_BBOXES,
        )

        published = {key: tuple(box) for key, box in self.vendored["US_STATE_BBOXES"].items()}
        self.assertEqual(REDATA_OVERTURE_SHARD_BBOXES, published)

    def test_the_table_is_vendored_from_the_same_redata_as_the_schema(self) -> None:
        self.assertEqual(self.vendored["x-redata-revision"], _load()["info"]["x-redata-revision"])
