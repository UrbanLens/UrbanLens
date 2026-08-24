"""OpenAPI schema scoping for the external API.

drf-spectacular walks every DRF view in the project by default; UrbanLens's
internal REST surface (``dashboard/rest/``) has no public contract and must not
leak into the published schema. The preprocessing hook below keeps schema
generation strictly to the mount points a third-party client is allowed to
call - nothing else.

"Allowed to call" is not the same as "lives under the external mount", which is
why :data:`PUBLISHED_SCHEMA_PREFIXES` is a tuple rather than a single string.
See :data:`E2EE_PREFIX`.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.settings import spectacular_settings

#: URL prefix of the external API mount.
EXTERNAL_API_PREFIX = "/dashboard/api/external/"

#: URL prefix of the end-to-end-encryption key-exchange mount.
#:
#: These eight views live under ``dashboard/e2ee/`` rather than the external
#: mount, and they are deliberately *not* mirrored under ``/api/external/v1/`` -
#: ``controllers.e2ee`` forbids duplicating them, and the reason is specific:
#: two copies of a key-exchange contract drift, and a drifted key-exchange
#: contract means somebody's messages stop decrypting. But they already accept
#: API-key and OAuth2 bearer credentials (they extend
#: :class:`~urbanlens.dashboard.external_api.mixins.DualAuthJsonView` and
#: declare per-method ``messages:*`` scopes), so a native client both can and
#: must call them at this path.
#:
#: Omitting them from the published schema is what made the mobile team believe
#: end-to-end encryption had never shipped: the endpoints worked, and the only
#: document they had to go by was silent about them. A capability that exists
#: but is undocumented is, from a client author's seat, a capability that does
#: not exist. Documenting the mount that already exists is the whole fix.
E2EE_PREFIX = "/dashboard/e2ee/"

#: Every mount whose endpoints belong in the published contract, as
#: ``(url prefix, path-prefix regex)``.
#:
#: The two halves are declared together so a new mount cannot be added to one
#: and forgotten in the other - they are consumed by the endpoint filter and by
#: :func:`_pin_schema_path_prefix` respectively, and a mount present in only one
#: produces a document that is either over-published or misnamed.
#:
#: The url prefixes are matched with ``str.startswith``, so each must be an
#: absolute path ending in ``/``. Both properties are load-bearing. Anchoring at
#: the start is what keeps ``/dashboard/rest/`` - which shares the
#: ``/dashboard/`` root with everything here - out of a document that would
#: otherwise publish it. The trailing slash is what stops a prefix from claiming
#: a sibling mount whose name merely starts with the same letters
#: (``/dashboard/e2ee-debug/``).
#:
#: The regexes carry the *version* segment that the url prefixes deliberately
#: omit: ``/pins/`` must yield the tag ``pins``, not ``v1``, so everything up to
#: and including the version has to be stripped for tag extraction even though
#: the filter wants to admit every version at once.
_PUBLISHED_MOUNTS: tuple[tuple[str, str], ...] = (
    (EXTERNAL_API_PREFIX, r"/dashboard/api/external/v[0-9]+"),
    (E2EE_PREFIX, r"/dashboard/e2ee"),
)

#: URL prefixes admitted into the published schema.
PUBLISHED_SCHEMA_PREFIXES: tuple[str, ...] = tuple(prefix for prefix, _pattern in _PUBLISHED_MOUNTS)

#: Alternation of the same mounts, for drf-spectacular's ``SCHEMA_PATH_PREFIX``.
#:
#: Anchored here, and grouped, deliberately. drf-spectacular prepends ``^`` to
#: this value only when it does not already start with one - and a bare ``^``
#: in front of an alternation binds to the *first* branch alone, leaving every
#: later branch free to match mid-path. Writing ``^(?:a|b)`` ourselves is what
#: makes each branch anchored rather than only the first.
SCHEMA_PATH_PREFIX_PATTERN = "^(?:" + "|".join(pattern for _prefix, pattern in _PUBLISHED_MOUNTS) + ")"


def _pin_schema_path_prefix() -> None:
    """Stop drf-spectacular from guessing the common path prefix.

    When ``SCHEMA_PATH_PREFIX`` is unset, drf-spectacular estimates it as the
    longest path shared by every endpoint it is generating, then strips that
    prefix to derive each operation's ``operationId`` and its Swagger UI tag.

    With one published mount that estimate happened to be right, so nobody had
    to think about it. The moment a second mount joined, the longest shared path
    collapsed from ``/dashboard/api/external/v1`` to ``/dashboard``, and every
    operation in the document silently renamed itself - ``pins_retrieve`` became
    ``api_external_v1_pins_retrieve``, and every tag in the API collapsed into a
    single bucket called ``api``. That is a breaking change to generated client
    code, delivered as a side effect of a documentation fix, which is precisely
    the failure this whole module exists to prevent.

    Pinning the prefix to the mounts we actually publish keeps the existing
    operation ids and tags exactly as they were, and gives the e2ee operations
    the same treatment (``keys_retrieve``, tag ``keys``) instead of a
    ``e2ee_``-prefixed second naming convention.

    The assignment is skipped when the value is already set, so an explicit
    ``SPECTACULAR_SETTINGS["SCHEMA_PATH_PREFIX"]`` - the natural home for this,
    once someone owns that file - wins over this fallback rather than fighting
    it. ``spectacular_settings`` has no reload-on-``override_settings``
    receiver, so this is a stable one-time write, and the supported
    ``SpectacularAPIView(custom_settings=...)`` override path saves and restores
    the value around itself.
    """
    if spectacular_settings.SCHEMA_PATH_PREFIX is None:
        spectacular_settings.SCHEMA_PATH_PREFIX = SCHEMA_PATH_PREFIX_PATTERN


def preprocess_external_api_only(endpoints: list, **_kwargs) -> list:
    """drf-spectacular preprocessing hook: keep only publicly-contracted endpoints.

    Runs from ``EndpointEnumerator.get_api_endpoints``, before the generator
    decides on a path prefix - which is why :func:`_pin_schema_path_prefix` is
    called from here and not at import time. Doing it as an import side effect
    would fire whenever anything merely referenced this module.

    Args:
        endpoints: ``(path, path_regex, method, callback)`` tuples for every
            discovered endpoint.
        **_kwargs: Future-proofing for extra hook arguments.

    Returns:
        The endpoints under one of :data:`PUBLISHED_SCHEMA_PREFIXES`, in the
        order they were discovered.
    """
    _pin_schema_path_prefix()
    return [(path, path_regex, method, callback) for path, path_regex, method, callback in endpoints if path.startswith(PUBLISHED_SCHEMA_PREFIXES)]


#: Component name for the shared error body.
ERROR_SCHEMA_NAME = "ErrorResponse"

#: The envelope every refusal actually uses. DRF's own body is ``{"detail": ...}``;
#: ``external_api.mixins`` rewrites it, and a generated client that has to
#: special-case which endpoints use which shape is a client that will get it
#: wrong somewhere.
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

    The published document described only success. Every authenticated endpoint
    returns 401 without credentials and 403 when the key's scopes do not cover
    the call, and every endpoint addressed by a path parameter returns 404 for
    an id that does not resolve - none of which appeared in the schema. A client
    generated from it therefore had no branch for the most likely failures it
    would ever meet, and a strict generated client treats an undeclared status
    as a protocol violation rather than as "your token expired".

    Done as a postprocessing hook rather than per view because the omission is
    not per view: responses are not declared individually anywhere, so declaring
    them individually would be ~284 edits that the next endpoint would forget.

    ``setdefault`` throughout, so a view that documents its own 401 - with a
    better description, or a different shape - keeps it.

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

    Without this, drf-spectacular logged "could not resolve authenticator" for
    every external-API view - some 200 warnings - and, far worse, emitted a
    schema documenting **no authentication at all**, so a native client
    generated from it had no idea an ``Authorization: Bearer ulk_...`` header
    was required. Registration happens on import; this module is already
    imported by the schema build via ``PREPROCESSING_HOOKS``.
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
