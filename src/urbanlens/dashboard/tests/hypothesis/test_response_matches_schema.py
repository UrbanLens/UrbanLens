"""Responses have to match the schema the API publishes for them."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: Endpoints checked on every run, as ``(url name, kwargs or None)``.
#:
#: Chosen for shape rather than importance: a paging envelope, a bare object, a
#: custom envelope, and a list. Between them they cover the ways a response can
#: be declared, which is what this is really testing.
_ENDPOINTS: list[tuple[str, dict[str, Any] | None]] = [
    ("external_api:whoami", None),
    ("external_api:labels", None),
    ("external_api:undo", None),
    ("external_api:trips", None),
    ("external_api:custom_fields", None),
    ("external_api:saved_filters", None),
    ("external_api:notifications", None),
]


def _bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def openapi_to_json_schema(node: Any) -> Any:
    """Translate OpenAPI 3.0's ``nullable`` into something JSON Schema understands.

    Necessary, and a trap worth knowing about.

    Args:
        node: Any fragment of the OpenAPI document.

    Returns:
        The same fragment with ``nullable`` folded into ``type``, recursively."""
    if isinstance(node, list):
        return [openapi_to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    converted = {key: openapi_to_json_schema(value) for key, value in node.items() if key != "nullable"}
    if not node.get("nullable"):
        return converted

    declared = converted.get("type")
    if declared is None:
        # A nullable `$ref`, which drf-spectacular emits as an `allOf` with no
        # `type` of its own. A sibling keyword cannot express "or null" here, so
        # the whole subschema becomes a choice.
        return {"anyOf": [converted, {"type": "null"}]}
    converted["type"] = [*declared, "null"] if isinstance(declared, list) else [declared, "null"]
    return converted


class NullableTranslationTests(TestCase):
    """The translation must loosen exactly one thing and nothing else.

    Folding `nullable` into `type` makes the schema more permissive, and a conversion that overshot - dropping
    types, making everything optional - would leave a test that passes against any response at all."""

    databases: set[str] = set()

    def test_a_nullable_field_accepts_null(self) -> None:
        import jsonschema

        schema = openapi_to_json_schema(
            {"type": "object", "properties": {"next": {"type": "string", "nullable": True}}}
        )

        jsonschema.validate(instance={"next": None}, schema=schema)

    def test_a_nullable_field_still_rejects_the_wrong_type(self) -> None:
        import jsonschema

        schema = openapi_to_json_schema(
            {"type": "object", "properties": {"next": {"type": "string", "nullable": True}}}
        )

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance={"next": 42}, schema=schema)

    def test_a_field_that_is_not_nullable_still_rejects_null(self) -> None:
        """The whole point: only fields marked nullable become nullable."""
        import jsonschema

        schema = openapi_to_json_schema({"type": "object", "properties": {"count": {"type": "integer"}}})

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance={"count": None}, schema=schema)

    def test_required_properties_are_still_enforced(self) -> None:
        """The `labels/` defect was a missing required field, so this must hold."""
        import jsonschema

        schema = openapi_to_json_schema(
            {"type": "object", "required": ["uuid"], "properties": {"uuid": {"type": "string"}}}
        )

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance={}, schema=schema)

    def test_a_declared_array_still_rejects_an_object(self) -> None:
        """The `undo/` defect: a bare array declared, an envelope returned."""
        import jsonschema

        schema = openapi_to_json_schema({"type": "array", "items": {"type": "string"}})

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(instance={"entries": [], "omitted": []}, schema=schema)


class ResponseSchemaConformanceTests(TestCase):
    """Each endpoint's 200 body must validate against its declared schema."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        api_key, self.raw_key = generate_api_key(self.user, "schema-conformance")
        api_key.scopes = list(ApiKeyScope.values)
        api_key.save(update_fields=["scopes"])

        # One real row, so a list endpoint validates a populated response rather
        # than an empty one. An empty list satisfies almost any schema.
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), parent_pin=None)

    @staticmethod
    def _document() -> dict:
        from drf_spectacular.generators import SchemaGenerator

        return SchemaGenerator().get_schema(request=None, public=True)

    @staticmethod
    def _response_schema(document: dict, path: str) -> dict | None:
        """The schema declared for `path`'s 200 response, ready to validate against.

        Args:
            document: The generated OpenAPI document. path: The URL path to look up.

        Returns:
            A JSON Schema with the document's components attached so ``$ref`` resolves, or None when the operation declares no JSON 200 body."""
        operation = (document.get("paths", {}).get(path) or {}).get("get")
        if not operation:
            return None
        content = ((operation.get("responses") or {}).get("200") or {}).get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
        if not schema:
            return None
        # `$ref`s in the response point at `#/components/schemas/...`, so the
        # components have to travel with the fragment being validated - and the
        # whole thing needs translating out of OpenAPI 3.0 first.
        return openapi_to_json_schema({**schema, "components": document.get("components", {})})

    def test_every_listed_endpoint_matches_its_declared_response(self) -> None:
        import jsonschema

        document = self._document()
        failures: list[str] = []

        for url_name, kwargs in _ENDPOINTS:
            path = reverse(url_name, kwargs=kwargs)
            schema = self._response_schema(document, path)
            if schema is None:
                failures.append(
                    f"{path}: the schema declares no JSON 200 response, so a client has nothing to generate against"
                )
                continue

            response = self.client.get(path, headers=_bearer(self.raw_key))
            if response.status_code != 200:
                failures.append(f"{path}: answered {response.status_code}, so its declared 200 body was never checked")
                continue

            try:
                jsonschema.validate(instance=response.json(), schema=schema)
            except jsonschema.ValidationError as error:
                # `error.message` alone omits where in the body it happened,
                # which is the half that tells you which field drifted.
                location = "/".join(str(part) for part in error.absolute_path) or "(root)"
                failures.append(f"{path}: at {location}: {error.message}")

        self.assertFalse(
            failures,
            "responses do not match the schema the API publishes for them:\n  " + "\n  ".join(failures),
        )
