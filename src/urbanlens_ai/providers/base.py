"""Common contract every provider adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens_ai.schema import InferenceRequest, InferenceResponse


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
