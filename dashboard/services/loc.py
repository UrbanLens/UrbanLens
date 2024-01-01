"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    loc.py                                                                                               *
*        Path:    /dashboard/services/loc.py                                                                           *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import requests

class LOCJsonGateway:
    """
    Gateway for accessing JSON data from the Library of Congress.
    """

    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://loc.gov/api"

    def search_collections(self, query):
        """
        Search the Library of Congress collections.
        """
        url = f"{self.base_url}/search.json"
        params = {'q': query}
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()
