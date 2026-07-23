"""OpenAPI schema scoping for the external API.

drf-spectacular walks every DRF view in the project by default; UrbanLens's
internal REST surface (``dashboard/rest/``) has no public contract and must
not leak into the published schema. The preprocessing hook below keeps schema
generation strictly to the external API mount point, so the schema documents
exactly the surface a third-party client is allowed to call - nothing else.
"""

from __future__ import annotations

#: URL prefix of the external API mount - everything else is excluded from the schema.
EXTERNAL_API_PREFIX = "/dashboard/api/external/"


def preprocess_external_api_only(endpoints: list, **_kwargs) -> list:
    """drf-spectacular preprocessing hook: keep only external-API endpoints.

    Args:
        endpoints: ``(path, path_regex, method, callback)`` tuples for every
            discovered endpoint.
        **_kwargs: Future-proofing for extra hook arguments.

    Returns:
        The endpoints under :data:`EXTERNAL_API_PREFIX`, schema routes excluded.
    """
    return [(path, path_regex, method, callback) for path, path_regex, method, callback in endpoints if path.startswith(EXTERNAL_API_PREFIX)]
