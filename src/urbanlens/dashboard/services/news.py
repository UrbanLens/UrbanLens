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
import requests

from urbanlens.dashboard.services.gateway import Gateway


class NewsGateway(Gateway):
    """
    Gateway for a News API.
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://newsapi.google.com/v2/everything"  # Google News API URL

    def get_news(self, pin):
        """
        Fetch recent news articles about the given pin from the News API.
        """
        params = {
            "q": pin,
            "apiKey": self.api_key,
        }

        response = requests.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()

        return response.json().get("articles", [])
