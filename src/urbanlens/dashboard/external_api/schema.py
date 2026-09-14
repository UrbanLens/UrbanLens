"""OpenAPI schema scoping for the external API.

drf-spectacular walks every DRF view in the project by default; UrbanLens's internal REST surface
(``dashboard/rest/``) has no public contract and must not leak into the published schema.
"Allowed to call" is not the same as "lives under the external mount", which is why
:data:`PUBLISHED_SCHEMA_PREFIXES` is a tuple rather than a single string.
"""

from __future__ import annotations

import threading

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.settings import spectacular_settings

#: URL prefix of the external API mount.
EXTERNAL_API_PREFIX = "/dashboard/api/external/"

#: URL prefix of the end-to-end-encryption key-exchange mount. Omitting them from the published schema is what
#: made the mobile team believe end-to-end encryption had never shipped: the endpoints worked, and the only
#: document they had to go by was silent about them.
E2EE_PREFIX = "/dashboard/e2ee/"

#: Every mount whose endpoints belong in the published contract, as ``(url prefix, path-prefix regex)``.
#: Anchoring at the start is what keeps ``/dashboard/rest/`` - which shares the ``/dashboard/`` root with
#: everything here - out of a document that would otherwise publish it.
_PUBLISHED_MOUNTS: tuple[tuple[str, str], ...] = (
    (EXTERNAL_API_PREFIX, r"/dashboard/api/external/v[0-9]+"),
    (E2EE_PREFIX, r"/dashboard/e2ee"),
)

#: URL prefixes admitted into the published schema.
PUBLISHED_SCHEMA_PREFIXES: tuple[str, ...] = tuple(prefix for prefix, _pattern in _PUBLISHED_MOUNTS)

#: Alternation of the same mounts, for drf-spectacular's ``SCHEMA_PATH_PREFIX``. Writing ``^(?:a|b)`` ourselves
#: is what makes each branch anchored rather than only the first.
SCHEMA_PATH_PREFIX_PATTERN = "^(?:" + "|".join(pattern for _prefix, pattern in _PUBLISHED_MOUNTS) + ")"


def _pin_schema_path_prefix() -> None:
    """Stop drf-spectacular from guessing the common path prefix.

    Pinning the prefix to the mounts we actually publish keeps the existing operation ids and tags
    exactly as they were, and gives the e2ee operations the same treatment (``keys_retrieve``, tag
    ``keys``) instead of a ``e2ee_``-prefixed second naming convention.
    """
    if spectacular_settings.SCHEMA_PATH_PREFIX is None:
        spectacular_settings.SCHEMA_PATH_PREFIX = SCHEMA_PATH_PREFIX_PATTERN


def preprocess_external_api_only(endpoints: list, **_kwargs) -> list:
    """drf-spectacular preprocessing hook: keep only publicly-contracted endpoints.

    Doing it as an import side effect would fire whenever anything merely referenced this module.

    Args:
        endpoints: ``(path, path_regex, method, callback)`` tuples for every discovered endpoint.
        **_kwargs: Future-proofing for extra hook arguments.

    Returns:
        The endpoints under one of: data:`PUBLISHED_SCHEMA_PREFIXES`, in the order they were discovered.
    """
    _pin_schema_path_prefix()
    return [(path, path_regex, method, callback) for path, path_regex, method, callback in endpoints if path.startswith(PUBLISHED_SCHEMA_PREFIXES)]


#: Component name for the shared error body.
ERROR_SCHEMA_NAME = "ErrorResponse"

#: The envelope every refusal actually uses. DRF's own body is ``{"detail": ...}``; ``external_api.mixins``
#: rewrites it, and a generated client that has to special-case which endpoints use which shape is a client that
#: will get it wrong somewhere.
_ERROR_COMPONENT = {
    "type": "object",
    "properties": {"error": {"type": "string", "description": "Human-readable reason the request was refused."}},
    "required": ["error"],
}

#: HTTP methods an OpenAPI path item can carry. Everything else in a path item
#: (``parameters``, ``summary``) is not an operation and must be skipped.
_OPERATION_KEYS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _error_response(description: str) -> dict:
    """One response entry pointing at the shared error component."""
    return {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ERROR_SCHEMA_NAME}"}}}}


def document_error_responses(result: dict, generator, request, public) -> dict:
    """Declare the refusals every operation can already produce.

    Done as a postprocessing hook rather than per view because the omission is not per view: responses
    are not declared individually anywhere, so declaring them individually would be ~284 edits that the
    next endpoint would forget.

    Args:
        result: The generated schema, mutated in place.
        generator: drf-spectacular's generator (unused).
        request: The request the schema is being generated for (unused).
        public: Whether this is the public schema (unused).

    Returns:
        The schema, with error responses declared.
    """
    result.setdefault("components", {}).setdefault("schemas", {}).setdefault(ERROR_SCHEMA_NAME, _ERROR_COMPONENT)

    for path, path_item in result.get("paths", {}).items():
        # A templated segment is the only way an operation can be handed an
        # identifier that does not resolve.
        addressable = "{" in path
        for method, operation in path_item.items():
            if method.lower() not in _OPERATION_KEYS or not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            if operation.get("security"):
                responses.setdefault("401", _error_response("Authentication credentials were missing or invalid."))
                responses.setdefault("403", _error_response("The credential is valid but does not carry the scope this operation requires."))
            if addressable:
                responses.setdefault("404", _error_response("No such resource, or it is not visible to this caller."))
    return result


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """Documents ``ApiKeyAuthentication`` in the generated OpenAPI schema.

    Without this, drf-spectacular logged "could not resolve authenticator" for every external-API view -
    some 200 warnings - and, far worse, emitted a schema documenting **no authentication at all**, so a
    native client generated from it had no idea an ``Authorization: Bearer ulk_...`` header was
    required.
    Registration happens on import; this module is already imported by the schema build via
    ``PREPROCESSING_HOOKS``.
    """

    target_class = "urbanlens.dashboard.external_api.authentication.ApiKeyAuthentication"
    name = "apiKeyAuth"

    def get_security_definition(self, auto_schema):
        """The security scheme: HTTP bearer carrying a ``ulk_``-prefixed API key."""
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "UrbanLens API key (`ulk_...`), created in Settings -> API Keys. OAuth2 access tokens share the Bearer scheme and are documented separately.",
        }


#: Guards the mutation :func:`patch_extension_thread_safety` serializes.
_extension_load_lock = threading.Lock()


def patch_extension_thread_safety() -> None:
    """Serialize drf-spectacular's per-extension ``target_class`` resolution.

    Two schema requests arriving together (this app runs gevent workers, so genuinely concurrent
    requests to one process are ordinary) can both see the string, both start resolving, and one can
    read ``target_class`` mid-mutation - e.g. as the ``None`` the other's failed-import branch just
    wrote - raising ``AttributeError: 'NoneType' object has no attribute 'startswith'`` instead of
    producing a schema.
    """
    from drf_spectacular.plumbing import OpenApiGeneratorExtension

    unpatched = OpenApiGeneratorExtension.__dict__["_load_class"].__func__
    if getattr(unpatched, "_urbanlens_serialized", False):
        return

    def _serialized_load_class(cls: type) -> None:
        with _extension_load_lock:
            # Whoever got the lock first may have already resolved this exact
            # class while we were waiting for it.
            if isinstance(cls.target_class, str):  # type: ignore[attr-defined]
                unpatched(cls)

    _serialized_load_class._urbanlens_serialized = True  # type: ignore[attr-defined]  # noqa: SLF001 - our own marker, not drf-spectacular's
    OpenApiGeneratorExtension._load_class = classmethod(_serialized_load_class)  # type: ignore[assignment]  # noqa: SLF001 - the whole point is patching drf-spectacular's private
