"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    news.py                                                                                              *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2025-03-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess.a.mann@gmail.com                                                                                *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2025-03-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from dataclasses import dataclass, field

from urbanlens.dashboard.services.gateway import Gateway


@dataclass(frozen=True, slots=True, kw_only=True)
class NewsGateway(Gateway):
    """
    Gateway for a News API.
    """

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
