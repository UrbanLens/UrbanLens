"""Does the API do what its published schema says it does?

The external API's OpenAPI document is not a build artefact - it is served at
``schema/``, third parties generate clients from it, and the Flutter app is one
of them. That makes every mismatch between the document and the running code a
break in somebody else's build, discovered by them rather than by us.

Nothing else checks this. The Python suite asserts endpoint behaviour against
hand-written expectations, and the same author writes both, so a serializer and
its test can agree with each other while both disagree with the schema. The
Playwright ``api`` project checks that the document *generates* and describes
the right paths, not that responses match it. Schemathesis reads the document
as the specification it claims to be, generates inputs from it, and holds the
responses to it.

Run it with ``bin/run_contract_tests.sh``; see ``docs/CONTRACT_TESTS.md``.
"""

from __future__ import annotations

from collections import defaultdict

from hypothesis import HealthCheck, settings
import pytest
from schema_source import generate_schema_document, live_base_url, load_schema, max_examples, response_checks

pytestmark = pytest.mark.contract

#: Loaded once, at import: `parametrize` needs the operation list to build one
#: test per operation, and that happens during collection.
schema = load_schema()


@schema.parametrize()
@settings(
    max_examples=max_examples(),
    # A Django request that touches PostGIS is routinely slower than the 200ms
    # default, and a deadline breach here would be a statement about this
    # machine rather than about the contract.
    deadline=None,
    # Same reasoning applied to Hypothesis's own view of the run: generation is
    # cheap, the request is not, so "this is taking a while" is expected.
    #
    # function_scoped_fixture is suppressed because `contract_headers` is meant
    # to be per-operation rather than per-example. The health check exists to
    # catch a fixture whose reset the author assumed was happening between
    # examples; here the account is deliberately shared by every example in one
    # operation's test, and the surrounding transaction rolls all of it back.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_operation_matches_its_schema(case, contract_headers: dict[str, str]) -> None:
    """Every generated request produces a response the schema describes.

    Schemathesis's default checks cover the failures that actually reach client
    authors: a 500, a status code the operation never declared, a content type
    it never declared, and a body that does not validate against the declared
    response schema.

    Args:
        case: One generated request, supplied by ``schema.parametrize``.
        contract_headers: Bearer credentials for the account under test.
    """
    # Assigned onto the case rather than passed to `call_and_validate`, because
    # the operations declare a security scheme and schemathesis generates a
    # value for it - which wins over anything handed to the call. The generated
    # token is a random string, so every request came back 401 and every
    # operation "failed" for the same uninteresting reason.
    case.headers.update(contract_headers)
    case.call_and_validate(checks=response_checks())


@pytest.fixture(scope="module")
def document() -> dict:
    """The published schema, unfiltered by the method selection."""
    if live_base_url():
        pytest.skip("Document-shape checks read the generated schema; they add nothing in live mode.")
    return generate_schema_document()


class TestDocumentShape:
    """Properties of the document itself, independent of any response.

    These need no requests and no database, so they are cheap enough to be
    worth running even when the fuzzing is not.
    """

    def test_operation_ids_are_unique(self, document: dict) -> None:
        """No two operations may share an ``operationId``.

        This is the one property a generated client cannot survive losing.
        drf-spectacular does not fail on a collision - it appends ``_2`` to the
        loser and logs a warning - and *which* operation loses depends on the
        order the urlconf is walked. Add a route and the suffix can move to the
        other operation, silently renaming a method that downstream code calls.
        """
        seen: dict[str, list[str]] = defaultdict(list)
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                operation_id = operation.get("operationId") if isinstance(operation, dict) else None
                if operation_id:
                    seen[operation_id].append(f"{method.upper()} {path}")

        collisions = {operation_id: locations for operation_id, locations in seen.items() if len(locations) > 1}
        # `_2`-suffixed ids are the *evidence* of a collision drf-spectacular
        # already resolved, so report them alongside rather than treating the
        # resolved pair as unique.
        resolved = sorted(
            operation_id
            for operation_id in seen
            if operation_id.rstrip("0123456789").rstrip("_") in seen and operation_id[-1].isdigit()
        )

        detail = "\n".join(
            f"  {operation_id}: {', '.join(locations)}" for operation_id, locations in sorted(collisions.items())
        )
        if resolved:
            detail += "\n  auto-suffixed (the other half of a collision): " + ", ".join(resolved)
        assert not collisions and not resolved, f"operationId collisions rename generated client methods:\n{detail}"

    def test_every_operation_declares_a_response(self, document: dict) -> None:
        """An operation with no documented response describes nothing to generate against."""
        undocumented = [
            f"{method.upper()} {path}"
            for path, operations in document["paths"].items()
            for method, operation in operations.items()
            if isinstance(operation, dict) and not operation.get("responses")
        ]
        assert not undocumented, "operations with no declared responses:\n  " + "\n  ".join(sorted(undocumented))

    def test_authenticated_operations_declare_their_security(self, document: dict) -> None:
        """A documented endpoint that needs a key must say so.

        The schema once described the entire API as unauthenticated because
        drf-spectacular could not resolve ``ApiKeyAuthentication``; a client
        generated from it had no way to know a bearer token existed.
        ``ApiKeyAuthenticationScheme`` fixed that, and this notices if the
        registration is ever lost - which would look, from the document, like
        the whole API becoming public.
        """
        schemes = document.get("components", {}).get("securitySchemes", {})
        assert schemes, "the document declares no security schemes at all - the API reads as fully anonymous."

        secured = [
            path
            for path, operations in document["paths"].items()
            for operation in operations.values()
            if isinstance(operation, dict) and operation.get("security")
        ]
        assert secured, "no operation declares `security`; every endpoint reads as anonymous to a generated client."

    def test_authenticated_operations_document_rejection(self, document: dict) -> None:
        """An operation that can answer 401 has to say so.

        Every authenticated endpoint returns ``401`` to a request without
        credentials - correctly - and the schema documents only ``200``. A
        client generated from this document has no branch for the single most
        likely failure it will meet, and a strict generated client will treat
        the response as a protocol violation rather than as "your token
        expired".

        This is one assertion rather than a failure per operation because it is
        one omission: the responses are not declared per-view, so the fix is
        also one place - a drf-spectacular postprocessing hook, or
        ``extend_schema(responses=...)`` on the shared base view.
        """
        missing = sorted(
            f"{method.upper()} {path}"
            for path, operations in document["paths"].items()
            for method, operation in operations.items()
            if isinstance(operation, dict)
            and operation.get("security")
            and "401" not in (operation.get("responses") or {})
        )
        assert not missing, (
            f"{len(missing)} authenticated operations never document a 401, though every one of them returns it.\n  First few:\n    "
            + "\n    ".join(missing[:8])
        )
