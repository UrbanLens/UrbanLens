"""Common contract every provider adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens_ai.schema import ClassifyRequest, ClassifyResponse, InferenceRequest, InferenceResponse


class ProviderError(RuntimeError):
    """A provider call failed: network error, API error, or an unparseable response."""


class ProviderAdapter(ABC):
    """Translates a normalized :class:`InferenceRequest` to/from one provider's SDK."""

    @abstractmethod
    def send(self, request: InferenceRequest) -> InferenceResponse:
        """Call the provider and return a normalized response.

        Args:
            request: The validated, policy-checked inference request.

        Returns:
            The provider's answer, normalized to :class:`InferenceResponse`.

        Raises:
            ProviderError: The provider call failed.
        """
        raise NotImplementedError

    def classify(self, request: ClassifyRequest) -> ClassifyResponse:
        """Classify an image, returning scored labels.

        Not abstract: most providers have no classifier endpoint at all, and
        ``policy.validate_classify_request`` already refuses those before an
        adapter is built. This default is the belt-and-braces answer for a
        provider that slips past it, so an unimplemented path is a clean
        error rather than an ``AttributeError``.

        Args:
            request: The validated, policy-checked classification request.

        Returns:
            The provider's labels, normalized and highest-confidence first.

        Raises:
            ProviderError: This provider has no image classifier here.
        """
        raise ProviderError(f"{type(self).__name__} does not implement image classification")
