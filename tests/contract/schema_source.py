"""The OpenAPI document under test, and where the requests it drives are sent.

This module exists so the conformance tests do not have to care which of two
very different things they are pointed at:

*In-process* (the default). The schema is generated straight from the urlconf
and every generated request is handed to Django's WSGI callable. No server, no
network, no deployment - it runs anywhere ``pytest`` runs, and it is the mode a
pre-merge check would use.

*Live*. Set :data:`BASE_URL_ENV` and the schema is fetched from a deployment and
the requests go over HTTP, through the proxy, into the real process. Use it to
answer "does the thing we actually shipped still match its contract", which is
the question the in-process mode structurally cannot ask.

Why the in-process mode generates the document rather than fetching it from
``schema/`` over WSGI: the fetch would be a request, and a request means
middleware, ``ALLOWED_HOSTS`` and possibly the database, all at *collection*
time, before pytest has set anything up. drf-spectacular's generator is pure
introspection and applies the same preprocessing hooks the view does, so the
document is identical and cannot fail for reasons unrelated to the schema.
That the endpoint serves it is a separate claim, and one that
``test_external_api_schema_e2ee.py`` and the Playwright ``api`` project already
make.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

import schemathesis
from schemathesis import BaseSchema

#: Path the published schema is served at. Only used to label the live fetch and
#: to tell schemathesis where the document came from.
SCHEMA_PATH: Final[str] = "/dashboard/api/external/v1/schema/"

#: Set to a deployment's base URL to switch from in-process to live mode.
BASE_URL_ENV: Final[str] = "UL_CONTRACT_BASE_URL"

#: ``safe`` (the default) or ``all``. See :func:`selected_methods`.
METHODS_ENV: Final[str] = "UL_CONTRACT_METHODS"

#: Hypothesis examples generated per operation.
MAX_EXAMPLES_ENV: Final[str] = "UL_CONTRACT_MAX_EXAMPLES"

#: Raw ``ulk_`` key for live mode, if not taken from the accounts manifest.
API_KEY_ENV: Final[str] = "UL_CONTRACT_API_KEY"

#: Set to enable schemathesis's coverage phase. See :func:`_build_config`.
COVERAGE_ENV: Final[str] = "UL_CONTRACT_COVERAGE"

#: Set to also fail on undocumented status codes and content types.
STRICT_ENV: Final[str] = "UL_CONTRACT_STRICT"

#: The manifest ``provision_integration_env --out`` writes. Shared with the
#: Playwright suite rather than given a second format to drift from.
ACCOUNTS_FILE_ENV: Final[str] = "UL_E2E_ACCOUNTS_FILE"

#: Methods that must not change server state. Fuzzing these is safe anywhere,
#: which is why they are the default selection.
SAFE_METHODS: Final[tuple[str, ...]] = ("GET", "HEAD")

#: Default examples per operation. Low on purpose: this is a conformance check
#: over ~100 operations, not a search for a rare input. Raise it when hunting.
DEFAULT_MAX_EXAMPLES: Final[int] = 8


class ContractConfigurationError(RuntimeError):
    """Raised when the suite is asked to run in a way it cannot."""


def live_base_url() -> str | None:
    """The deployment to test, or ``None`` for in-process mode.

    Returns:
        The base URL with any trailing slashes removed, or ``None``.
    """
    raw = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    return raw or None


def selected_methods() -> tuple[str, ...] | None:
    """Which HTTP methods to generate requests for.

    ``safe`` restricts generation to :data:`SAFE_METHODS`. ``all`` lifts the
    restriction, which means the run will create, modify and delete data as
    whichever account it is authenticated as - fine against a throwaway test
    database, and fine against the disposable accounts
    ``provision_integration_env`` makes, but not something to point at an
    account anybody cares about.

    Returns:
        The methods to include, or ``None`` to include every method.

    Raises:
        ContractConfigurationError: If the variable holds neither value.
    """
    raw = os.environ.get(METHODS_ENV, "safe").strip().lower()
    if raw == "safe":
        return SAFE_METHODS
    if raw == "all":
        return None
    raise ContractConfigurationError(f"{METHODS_ENV} must be 'safe' or 'all', not {raw!r}.")


def max_examples() -> int:
    """Examples Hypothesis generates per operation.

    Returns:
        A positive example count.

    Raises:
        ContractConfigurationError: If the variable is not a positive integer.
    """
    raw = os.environ.get(MAX_EXAMPLES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_EXAMPLES
    try:
        value = int(raw)
    except ValueError as error:
        raise ContractConfigurationError(f"{MAX_EXAMPLES_ENV} must be an integer, not {raw!r}.") from error
    if value < 1:
        raise ContractConfigurationError(f"{MAX_EXAMPLES_ENV} must be at least 1, not {value}.")
    return value


def generate_schema_document() -> dict[str, Any]:
    """Build the published OpenAPI document without issuing a request.

    Returns:
        The same document ``external_api:schema`` serves, as a dict.
    """
    # Imported here rather than at module scope: this module is imported during
    # collection, and drf-spectacular pulls in the urlconf, which must not
    # happen before pytest-django has configured settings.
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


def coverage_enabled() -> bool:
    """Whether to run schemathesis's coverage phase."""
    return os.environ.get(COVERAGE_ENV, "").strip().lower() in {"1", "true", "yes"}


def strict_enabled() -> bool:
    """Whether to fail on undocumented status codes and content types."""
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes"}


def response_checks() -> list:
    """The checks each response is held to.

    The default pair is the part that is about *this* application's behaviour:
    it must not 500, and a body it returns must validate against the schema it
    published for that response. Both are green today, so a failure means
    something drifted.

    ``status_code_conformance`` and ``content_type_conformance`` are strong
    checks that this schema is not yet ready for, because it documents only
    success. A GET on ``pins/{pin_slug}/`` with a generated slug correctly
    returns 404, which is undocumented, so the check fires on a correct
    response - and it would fire on almost every parameterised operation. Turn
    them on with :data:`STRICT_ENV` once the error responses are declared; the
    document-shape tests measure how far off that is.

    ``ignored_auth`` is excluded in both modes. It works by re-sending a request
    with the credential removed or corrupted, but it can only tamper with a
    credential it generated - and this suite deliberately supplies a real one
    outside that model, so the check sees its "invalid" request succeed and
    reports an authentication bypass that is not there. Auth rejection is
    covered for real by the Playwright suite's ``api/auth.spec.ts``.

    Returns:
        Check callables to pass to ``call_and_validate``.
    """
    from schemathesis.checks import not_a_server_error
    from schemathesis.specs.openapi.checks import (
        content_type_conformance,
        response_schema_conformance,
        status_code_conformance,
    )

    checks = [not_a_server_error, response_schema_conformance]
    if strict_enabled():
        checks += [status_code_conformance, content_type_conformance]
    return checks


def _build_config():
    """Configuration shared by both modes.

    Two deliberate restrictions, both because of what this suite is *for*.

    ``with_security_parameters=False`` stops schemathesis generating a value for
    the declared bearer scheme. Left on, it invents a random token, and every
    operation then fails with 401 for a reason that says nothing about the
    contract. The real credential is applied per-test instead.

    The coverage phase is off by default. It is the part that deliberately drops
    a required header, sends undeclared methods and mistypes parameters - all
    genuinely useful, and all of which this API answers with a status its schema
    does not document (401, 405), so every operation fails for the same systemic
    reason. That gap is asserted once, precisely, by
    ``TestDocumentShape.test_authenticated_operations_document_rejection``
    rather than a hundred times here. Set :data:`COVERAGE_ENV` to run it
    anyway, which is worth doing when auditing error responses.
    """
    from schemathesis import Config, GenerationMode
    from schemathesis.config import CoveragePhaseConfig, GenerationConfig, PhasesConfig, ProjectConfig, ProjectsConfig

    project = ProjectConfig(
        generation=GenerationConfig(modes=[GenerationMode.POSITIVE], with_security_parameters=False),
        phases=PhasesConfig(coverage=CoveragePhaseConfig(enabled=coverage_enabled())),
    )
    return Config(projects=ProjectsConfig(default=project))


def _in_process_schema() -> BaseSchema:
    """Load the schema and point schemathesis at Django's WSGI callable."""
    from django.core.wsgi import get_wsgi_application

    schema = schemathesis.openapi.from_dict(generate_schema_document(), config=_build_config())
    # `transport` is chosen from `app`, so assigning it is what makes generated
    # calls go through WSGI instead of over the network. This is exactly what
    # `schemathesis.openapi.from_wsgi` does after it fetches the document.
    #
    # `get_wsgi_application()` rather than importing `UrbanLens.wsgi`: the
    # deployed module is entitled to have import-time side effects aimed at a
    # server process, and none of them are wanted inside a test runner.
    schema.app = get_wsgi_application()
    schema.location = SCHEMA_PATH
    return schema


def _live_schema(base_url: str) -> BaseSchema:
    """Fetch the schema from a deployment and address it over HTTP."""
    return schemathesis.openapi.from_url(f"{base_url}{SCHEMA_PATH}?format=json", config=_build_config())


def load_schema() -> BaseSchema:
    """The schema the conformance tests are parametrised over.

    Returns:
        A schemathesis schema, filtered to :func:`selected_methods`.
    """
    base_url = live_base_url()
    schema = _live_schema(base_url) if base_url else _in_process_schema()

    methods = selected_methods()
    if methods is not None:
        schema = schema.include(method=list(methods))
    return schema


def manifest_api_key() -> str | None:
    """The primary account's API key, from the shared accounts manifest.

    Returns:
        A raw ``ulk_`` key, or ``None`` when no manifest is configured or the
        manifest holds no key.

    Raises:
        ContractConfigurationError: If the manifest is named but unreadable.
    """
    location = os.environ.get(ACCOUNTS_FILE_ENV, "").strip()
    if not location:
        return None

    path = Path(location)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContractConfigurationError(
            f"{ACCOUNTS_FILE_ENV} points at {path}, which could not be read as JSON: {error}"
        ) from error

    accounts = manifest.get("accounts") or []
    for account in accounts:
        if account.get("role") == "primary" and account.get("api_key"):
            return str(account["api_key"])
    for account in accounts:
        if account.get("api_key"):
            return str(account["api_key"])
    return None
