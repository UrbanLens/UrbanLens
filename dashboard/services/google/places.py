"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    places.py                                                                                            *
*        Path:    /dashboard/services/google/places.py                                                                 *
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
from dashboard.services.gateway import Gateway

class GooglePlacesGateway(Gateway):
    """
    Gateway for the Google Places API.
    """
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()

    def get_data(self, latitude, longitude, radius=1000, place_type=None):
        """
        Fetch details about locations near the given coordinates from Google Places API.
        """
        base_url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
        params = {
            'location': f'{latitude},{longitude}',
            'radius': radius,
            'key': self.api_key
        }

        if place_type:
            params['type'] = place_type

        response = self.session.get(base_url, params=params)
        response.raise_for_status()
        
        places_data = response.json().get('results', [])
        return places_data

    def get_place_details(self, place_id, fields=None):
        details_url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'key': self.api_key
        }
        if fields:
            params['fields'] = ','.join(fields)
        
        response = self.session.get(details_url, params=params)
        response.raise_for_status()
        return response.json().get('result', {})

    def get_place_photos(self, photoreference, max_width=None):
        photo_url = 'https://maps.googleapis.com/maps/api/place/photo'
        params = {
            'photoreference': photoreference,
            'key': self.api_key
        }
        if max_width:
            params['maxwidth'] = max_width
        
        response = self.session.get(photo_url, params=params, stream=True)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image.
    
    def autocomplete(self, input_text):
        autocomplete_url = 'https://maps.googleapis.com/maps/api/place/autocomplete/json'
        params = {
            'input': input_text,
            'key': self.api_key
        }
        
        response = self.session.get(autocomplete_url, params=params)
        response.raise_for_status()
        return response.json().get('predictions', [])