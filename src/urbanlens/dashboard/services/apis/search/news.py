from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from urbanlens.dashboard.services.gateway import Gateway


@dataclass(slots=True, kw_only=True)
class NewsGateway(Gateway):
    """
    Gateway for a News API.
    """

    service_key: ClassVar[str] = "news"
    paid_service: ClassVar[bool] = True

    api_key: str
    base_url: str = "https://newsapi.google.com/v2/everything"  # Google News API URL

    def get_news(self, pin: int | str) -> list[dict]:
        """
        Fetch recent news articles about the given pin from the News API.
        """
        params = {
            "q": pin,
            "apiKey": self.api_key,
        }

        response = self.session.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()

        return response.json().get("articles", [])
