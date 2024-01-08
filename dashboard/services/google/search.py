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
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from django.conf import settings
import requests
from dashboard.services.gateway import Gateway

class GoogleCustomSearchGateway(Gateway):
    """
    Gateway for the Google Custom Search API.
    """

    def __init__(self):
        self.api_key = settings.GOOGLE_SEARCH_API_KEY
        #self.cx = cx
        self.base_url = "https://www.googleapis.com/customsearch/v1"

    def search(self, query : str, max_results : int = 20) -> dict:
        """
        Perform a search using the Google Custom Search API.
        """
        params = {
            'key': self.api_key,
            #'cx': self.cx,
            'q': query,
            'num': min(max_results, 20)
        }
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        return response.json()

    def parse_response(self, data):
        """
        Extract search results from the API response.
        """
        results = []
        for item in data.get('items', []):
            result = {
                'title': item.get('title'),
                'link': item.get('link'),
                'snippet': item.get('snippet')
            }
            results.append(result)
        return results