"""Properties the published OpenAPI document must hold as a whole.

The schema tests that already existed assert that *particular* paths are present
or absent - the e2ee mount is published, `/dashboard/rest/` is not. Nothing
asserted anything about the document globally, and that is the gap two findings
came through on 2026-08-24: two pairs of operations shared an `operationId`, and
no authenticated operation documented the 401 it returns. Both are properties of
every operation at once, so no per-endpoint test was ever going to notice.

Mirrored from `tests/contract/test_openapi_conformance.py` on purpose. That
suite is outside `testpaths` and needs an explicit invocation, so a check living
only there does not run on a normal `pytest`. These are the cheap half - pure
introspection, no database, no HTTP - and they are the half worth having on
every commit. See `docs/audits/TEST_COVERAGE_GAPS.md`.
"""

from __future__ import annotations

from collections import defaultdict

from django.test import SimpleTestCase

#: Keys in an OpenAPI path item that describe an operation. Everything else
#: there (`parameters`, `summary`) is not one and must be skipped.
_OPERATION_KEYS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _document() -> dict:
    """Generate the published schema without issuing a request.

    Imported inside the function rather than at module scope: drf-spectacular
    pulls in the urlconf, and that should happen when a test runs rather than
    when this module is collected.

    Returns:
        The same document ``external_api:schema`` serves.
    """
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


def _operations(document: dict):
    """Yield ``(path, method, operation)`` for every real operation."""
    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() in _OPERATION_KEYS and isinstance(operation, dict):
                yield path, method, operation


class OperationIdTests(SimpleTestCase):
    """`operationId` is the name generated client code calls."""

    def test_no_two_operations_share_an_operation_id(self) -> None:
        """A collision renames somebody's method without anybody deciding to.

        drf-spectacular does not fail on a duplicate - it appends ``_2`` to
        whichever operation it reaches second and logs a warning nobody reads.
        Which one loses depends on the order the urlconf is walked, so adding an
        unrelated route can move the suffix to the other operation and silently
        rename a method downstream code calls.
        """
        document = _document()
        seen: dict[str, list[str]] = defaultdict(list)
        for path, method, operation in _operations(document):
            operation_id = operation.get("operationId")
            if operation_id:
                seen[operation_id].append(f"{method.upper()} {path}")

        collisions = {name: where for name, where in seen.items() if len(where) > 1}
        # A `_2` suffix is the *evidence* of a collision drf-spectacular already
        # resolved, so the resolved pair has to be caught too - by then the ids
        # are unique and the damage is done.
        suffixed = sorted(name for name in seen if name[-1].isdigit() and name.rstrip("0123456789").rstrip("_") in seen)

        detail = "\n".join(f"  {name}: {', '.join(where)}" for name, where in sorted(collisions.items()))
        if suffixed:
            detail += "\n  auto-suffixed (the other half of a collision): " + ", ".join(suffixed)
        self.assertFalse(collisions or suffixed, f"operationId collisions rename generated client methods:\n{detail}")


class DocumentedRefusalTests(SimpleTestCase):
    """An operation has to document the ways it says no."""

    def test_authenticated_operations_document_401_and_403(self) -> None:
        """A client needs a branch for the failure it will meet most often.

        Every authenticated endpoint answers 401 without credentials and 403
        when the credential's scopes do not cover the call. A document that
        mentions neither leaves a generated client treating both as protocol
        violations rather than as "your token expired" and "you cannot do that".
        """
        document = _document()
        missing: list[str] = []
        for path, method, operation in _operations(document):
            if not operation.get("security"):
                continue
            responses = operation.get("responses") or {}
            absent = [code for code in ("401", "403") if code not in responses]
            if absent:
                missing.append(f"{method.upper()} {path} (missing {', '.join(absent)})")

        self.assertFalse(
            missing,
            f"{len(missing)} authenticated operations do not document the refusals they return:\n  "
            + "\n  ".join(missing[:10]),
        )

    def test_operations_with_a_path_parameter_document_404(self) -> None:
        """An id that resolves to nothing is the other everyday failure."""
        document = _document()
        missing = [
            f"{method.upper()} {path}"
            for path, method, operation in _operations(document)
            if "{" in path and "404" not in (operation.get("responses") or {})
        ]

        self.assertFalse(
            missing,
            f"{len(missing)} operations addressed by a path parameter do not document a 404:\n  "
            + "\n  ".join(missing[:10]),
        )

    def test_every_operation_declares_at_least_one_response(self) -> None:
        """An operation describing no response describes nothing to generate against."""
        document = _document()
        undocumented = [
            f"{method.upper()} {path}"
            for path, method, operation in _operations(document)
            if not operation.get("responses")
        ]

        self.assertFalse(undocumented, "operations with no declared responses:\n  " + "\n  ".join(sorted(undocumented)))


class PublishedSurfaceTests(SimpleTestCase):
    """What the document is allowed to describe at all."""

    def test_the_internal_rest_surface_is_never_published(self) -> None:
        """`/dashboard/rest/` has no public contract and must not appear.

        Asserted here as well as in the existing e2ee-prefix tests because this
        one is about the *whole* document: a preprocessing hook that stopped
        filtering would leak the internal API without any single path test
        noticing.
        """
        document = _document()
        leaked = [path for path in document.get("paths", {}) if path.startswith("/dashboard/rest/")]

        self.assertFalse(leaked, "the published schema documents the internal REST surface:\n  " + "\n  ".join(leaked))

    def test_the_document_declares_a_security_scheme(self) -> None:
        """Losing the authentication extension makes the whole API read as public.

        It happened once: drf-spectacular could not resolve
        ``ApiKeyAuthentication`` and emitted a schema documenting no
        authentication at all, so a client generated from it had no idea a
        bearer token existed.
        """
        document = _document()
        schemes = document.get("components", {}).get("securitySchemes", {})

        self.assertTrue(schemes, "the document declares no security schemes - the API reads as fully anonymous.")
