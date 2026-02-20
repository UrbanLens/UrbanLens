"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    search.py                                                                                            *
*        Path:    /dashboard/services/google/search.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations

import logging
from typing import Any

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class GoogleCustomSearchGateway(Gateway):
    """
    Gateway for the Google Custom Search API.
    """

    def __init__(self):
        self.api_key = settings.google_search_api_key
        self.cx = settings.google_search_tenant
        self.base_url = "https://customsearch.googleapis.com/customsearch/v1"

    def search(self, terms: str | list[str | list[str]], max_results: int = 20) -> list[dict[str, Any]]:
        """
        Perform a search using the Google Custom Search API.
        """
        query = self.build_query(terms)

        headers = {
            "Referer": "http://localhost:8000",
        }
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            # 'num': min(max_results, 20)
        }
        response = requests.get(self.base_url, params=params, headers=headers)
        response.raise_for_status()
        return self.parse_response(response)

    def parse_response(self, response: requests.Response) -> list[dict[str, Any]]:
        """
        Extract search results from the API response.
        """
        data = response.json()

        results: list[dict[str, Any]] = []
        for item in data.get("items", []):
            result = {
                "title": item.get("title"),
                "link": item.get("link"),
                "snippet": item.get("snippet"),
            }
            results.append(result)
        return results

    def preprocess_query_terms(self, terms: list[str]) -> list[str]:
        """
        Build a query string from a list of search terms using OR.

        Args:
            terms (list[str]): A list of search terms.

        """
        # Join all terms with "OR", and wrap in quotes. Do not wrap terms that already have quotes, or begin with parenthesis
        query_terms = []

        for term in terms:
            if not term:
                continue

            term = term.strip()
            if term.startswith(('"', "(")):
                query_terms.append(term)
            else:
                # sanitize existing quotes in term
                value = term.replace('"', '\\"')
                query_terms.append(f'"{value}"')

        return query_terms

    def build_query_or(self, terms: list[str]) -> str:
        """
        Build a query string from a list of search terms using OR.

        Args:
            terms (list[str]): A list of search terms.

        """
        query_terms = self.preprocess_query_terms(terms)

        query = " OR ".join(query_terms)
        if len(query_terms) > 1:
            return f"({query})"
        return query

    def build_query_and(self, terms: list[str]) -> str:
        """
        Build a query string from a list of search terms using AND.

        Args:
            terms (list[str]): A list of search terms.

        """
        query_terms = self.preprocess_query_terms(terms)

        query = " AND ".join(query_terms)
        if len(query_terms) > 1:
            return f"({query})"
        return query

    def build_query(self, terms: str | list[str | list[str]]) -> str:
        """
        Accepts input like: 
        [
            'or_term1',
            'or_term2',
            [
                'and_term3', 
                'and_term4'
            ],
            'or_term5',
        ]
        Defaults to OR when combining lists
        """
        if isinstance(terms, str):
            return terms

        query_terms = []

        for term in terms:
            if isinstance(term, list):
                query_terms.append(self.build_query_and(term))
            else:
                query_terms.append(term)
            
        return self.build_query_or(query_terms)
