"""Everything this service refuses to do, in one place.

The proxy allowlist (``config/egress/``) is the actual network boundary - see
the plan's architecture note. This module is the mechanical check *inside*
the process: it validates an already-parsed request before any provider
client is built, so a caller that tries to smuggle a server-side tool, an
unlisted model, or an inflated ``max_tokens`` gets a clean 4xx instead of a
provider call that might have honored it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens_ai.schema import ClassifyRequest, InferenceRequest, Provider, ToolSpec

#: Per-request SDK timeout and retry budget. A hung provider connection must
#: not pin one of this service's ``-k gthread`` worker threads indefinitely -
#: see the architecture note on why this is a single-worker, multi-threaded
#: service. One retry (not zero) tolerates a single dropped connection
#: without doubling worst-case latency the way the SDK's own default
#: (several retries with backoff) would.
PROVIDER_TIMEOUT_SECONDS = 30
PROVIDER_MAX_RETRIES = 1

#: The official API host for each provider whose base URL is not otherwise
#: configurable. Passed explicitly to each SDK client rather than left to the
#: SDK's own default so an ``ANTHROPIC_BASE_URL``/``OPENAI_BASE_URL``
#: environment variable in this container's env cannot silently redirect
#: traffic to a look-alike host that happens to pass the egress-proxy filter.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
OPENAI_BASE_URL = "https://api.openai.com/v1"

#: Cloudflare has no single fixed API host - ``cloudflare_worker_ai_endpoint``
#: is account-specific and admin-configured (see ``SiteSettings.cloudflare_model``'s
#: own free-text design). It cannot be pinned to one constant, so it is
#: validated by shape instead: HTTPS, and a Cloudflare-owned host.
_CLOUDFLARE_HOST_RE = re.compile(r"^[a-z0-9.-]+\.cloudflare\.com$")

#: Every model each curated provider is allowed to serve, mirroring that
#: provider's own ``MODEL_COSTS`` catalog in ``dashboard.services.ai`` -
#: those two lists are kept in sync by hand; a model absent from both is
#: refused by this policy rather than silently costed at the generic
#: fallback estimate. Cloudflare is deliberately absent: ``SiteSettings.
#: cloudflare_model`` is free text so an admin can point at any Workers AI
#: model, so it is validated by shape (see ``_CLOUDFLARE_MODEL_RE``) instead
#: of an exact-match set.
ALLOWED_MODELS: dict[Provider, frozenset[str]] = {
    "anthropic": frozenset({"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}),
    "openai": frozenset({"gpt-5.2", "gpt-5-mini", "gpt-5-nano"}),
}

#: Cloudflare Workers AI model identifiers always look like ``@cf/vendor/name``.
_CLOUDFLARE_MODEL_RE = re.compile(r"^@cf/[a-z0-9_.-]+/[a-z0-9_.-]+$", re.IGNORECASE)

#: Hard ceiling on a single response's token budget, matching the one value
#: every existing AI feature already requests (``services.ai.meta.MAX_TOKENS``)
#: - not a new limit, just this service's own copy of the existing one so a
#: caller cannot ask for an unbounded response.
MAX_ALLOWED_TOKENS = 16000

#: Tool names naming a provider's own built-in, server-executed tool (web
#: search, code execution, computer use, ...) rather than a JSON-schema
#: function the caller implements itself. ``ToolSpec`` has no ``type`` field -
#: every tool this service is asked to declare is translated as a plain
#: function/custom tool, so there is structurally no way to request a
#: server-side tool through it - this list is the defense-in-depth check for
#: a caller trying anyway via the name, and what the "no web search" tests
#: exercise.
_SERVER_TOOL_NAMES = frozenset(
    {
        "web_search",
        "web_search_20250305",
        "computer",
        "computer_20250124",
        "bash",
        "bash_20250124",
        "text_editor",
        "text_editor_20250124",
        "code_execution",
        "code_execution_20250522",
    },
)


class PolicyError(ValueError):
    """A request violates this service's outbound-request policy.

    Raised (never a bare provider call) so the wsgi layer can return a clean
    400 without a provider ever seeing the request.
    """


def validate_model(provider: Provider, model: str) -> None:
    """Reject a model this service does not recognize for ``provider``.

    Args:
        provider: The target provider.
        model: The model identifier the caller requested.

    Raises:
        PolicyError: The model is not on the provider's allowlist (or, for
            Cloudflare, does not look like a Workers AI model identifier).
    """
    if provider == "cloudflare":
        if not _CLOUDFLARE_MODEL_RE.match(model):
            raise PolicyError(f"Model {model!r} does not look like a Cloudflare Workers AI model identifier")
        return

    allowed = ALLOWED_MODELS.get(provider, frozenset())
    if model not in allowed:
        raise PolicyError(f"Model {model!r} is not on the {provider} allowlist")


def validate_tools(tools: list[ToolSpec]) -> None:
    """Reject any tool that names a provider's built-in server-side tool.

    Args:
        tools: The tools the caller wants the model to have available.

    Raises:
        PolicyError: A tool name matches a known server-side tool identifier.
    """
    for tool in tools:
        if tool.name.lower() in _SERVER_TOOL_NAMES:
            raise PolicyError(f"Tool {tool.name!r} names a provider server-side tool, which this service never declares")


def validate_cloudflare_endpoint(endpoint: str) -> None:
    """Reject a Cloudflare Workers AI endpoint that isn't actually Cloudflare's.

    Args:
        endpoint: The configured ``cloudflare_worker_ai_endpoint`` URL.

    Raises:
        PolicyError: The URL is not HTTPS or its host is not ``*.cloudflare.com``.
    """
    if not endpoint.startswith("https://"):
        raise PolicyError("Cloudflare Workers AI endpoint must be HTTPS")
    host = endpoint.removeprefix("https://").split("/", 1)[0].split(":", 1)[0]
    if not _CLOUDFLARE_HOST_RE.match(host):
        raise PolicyError(f"Cloudflare Workers AI endpoint host {host!r} is not a cloudflare.com host")


#: Decoded size ceiling for a single inlined image. Callers send a
#: downscaled copy (512px longest edge, JPEG q80 - typically well under
#: 100 KB), so this is the backstop for a caller that forgets, not the
#: working limit. Enforced on the *decoded* length so a caller cannot slip a
#: large image past it by counting base64 characters.
MAX_IMAGE_BYTES = 1_500_000
#: Images allowed in one request. Every shipped vision caller sends exactly
#: one; more than a couple would blow the request cap anyway.
MAX_IMAGES_PER_REQUEST = 4

#: Providers whose adapters actually implement image input. Anthropic and
#: OpenAI take images as message content; Cloudflare takes them through its
#: own Workers AI payload shape. Kept as an explicit set so adding a fourth
#: provider fails closed on vision until its adapter is written.
_VISION_PROVIDERS = frozenset({"anthropic", "openai", "cloudflare"})

#: Cloudflare Workers AI is the only provider offering the image classifiers
#: this service exposes (ResNet-50 and friends) - OpenAI and Anthropic have
#: no equivalent endpoint, so a classify request naming them is a caller bug
#: rather than an unsupported-model case.
_CLASSIFY_PROVIDERS = frozenset({"cloudflare"})


def validate_image(image: object) -> None:
    """Reject an inlined image that is too large or not valid base64.

    Args:
        image: The :class:`~urbanlens_ai.schema.ImagePart` to check.

    Raises:
        PolicyError: The payload is not decodable base64, or decodes to more
            than :data:`MAX_IMAGE_BYTES`.
    """
    import base64
    import binascii

    data = getattr(image, "data", "")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PolicyError("Image data is not valid base64") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise PolicyError(f"Image is {len(decoded)} bytes, over the {MAX_IMAGE_BYTES} ceiling - send a downscaled copy")


def validate_classify_request(request: ClassifyRequest) -> None:
    """Run every check this service enforces on an image-classification call.

    Args:
        request: The normalized, already-schema-validated classify request.

    Raises:
        PolicyError: The provider does not offer classification here, the
            model identifier is not one this service recognizes, or the image
            failed :func:`validate_image`.
    """
    if request.provider not in _CLASSIFY_PROVIDERS:
        raise PolicyError(f"Provider {request.provider!r} does not offer image classification through this service")
    validate_model(request.provider, request.model)
    validate_image(request.image)


def validate_request(request: InferenceRequest) -> None:
    """Run every check this service enforces before building a provider client.

    Args:
        request: The normalized, already-schema-validated inference request.

    Raises:
        PolicyError: Any check failed - see the individual ``validate_*``
            functions for what each one covers.
    """
    validate_model(request.provider, request.model)
    validate_tools(request.tools)
    if request.provider == "cloudflare" and request.tools:
        raise PolicyError("Cloudflare Workers AI models are not offered tool use through this service")
    if request.max_tokens > MAX_ALLOWED_TOKENS:
        raise PolicyError(f"max_tokens={request.max_tokens} exceeds the {MAX_ALLOWED_TOKENS} ceiling")

    images = [image for message in request.messages for image in message.images]
    if not images:
        return
    if request.provider not in _VISION_PROVIDERS:
        raise PolicyError(f"Provider {request.provider!r} does not accept image input through this service")
    if len(images) > MAX_IMAGES_PER_REQUEST:
        raise PolicyError(f"{len(images)} images exceeds the {MAX_IMAGES_PER_REQUEST} per-request ceiling")
    for image in images:
        validate_image(image)
