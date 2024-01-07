"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    location.py                                                                                          *
*        Path:    /dashboard/controllers/location.py                                                                   *
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
import logging


from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings

from rest_framework.viewsets import GenericViewSet

from dashboard.models.locations.model import Location
from dashboard.services.smithsonian import SmithsonianGateway
from dashboard.services.google import GooglePlacesGateway


logger = logging.getLogger(__name__)

class LocationController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the location page
    """
    def view(self, request, location_id, *args, **kwargs):
        """
        Renders the location page.
        """
        # Get the location
        try:
            location : Location = Location.objects.get(id=location_id)
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(settings.SMITHSONIAN_API_KEY)
        google_places_gateway = GooglePlacesGateway(settings.GOOGLE_PLACES_API_KEY)

        # Smithsonian images will be loaded via htmx
        '''
        Sample smithsonian data: 
        [{'title': "Columnea 'Campus Sunset'", 'url': None, 'thumbnail': None}, {'title': 'Bicyclus campus', 'url': None, 'thumbnail': None}, {'title': 'Pacific studies', 'url': None, 'thumbnail': None}, {'title': 'Terry Adkins : sculpture and painting, East Campus Galleries, Valencia Community College', 'url': None, 'thumbnail': None}, {'title': 'Rethinking campus life new perspectives on the history of college students in the United States Christine A. Ogren, Marc A. VanOverbeke, editors', 'url': None, 'thumbnail': None}, {'title': 'Plestiodon fasciatus', 'url': None, 'thumbnail': None}, {'title': 'Osmorhiza occidentalis (Nutt.) Torr.', 'url': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m34b0d78659a4b4a69aaa8ed3053d1945c', 'thumbnail': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m34b0d78659a4b4a69aaa8ed3053d1945c/90'}, {'title': 'Heteropterys sp.', 'url': None, 'thumbnail': None}, {'title': 'Murraya exotica L.', 'url': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m384104033839044758020f2f5203a0554', 'thumbnail': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m384104033839044758020f2f5203a0554/90'}, {'title': 'Calophyllum inophyllum L.', 'url': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m39851075d3599483397693eacf141ceea', 'thumbnail': 'https://ids.si.edu/ids/deliveryService/id/ark:/65665/m39851075d3599483397693eacf141ceea/90'}]
        '''
        #google_places_images = google_places_gateway.get_data(location.latitude, location.longitude, radius=1000)
        #logger.critical('Google Places: %s', google_places_images)

        # Fetch the most recent search results for the location
        #recent_search_results = google_places_gateway.get_recent_search_results(location.name)
        #logger.critical('Recent search results: %s', recent_search_results)

        return render(request, 'dashboard/pages/location/index.html', {
            'location': location,
            'latitude': location.latitude,
            'longitude': location.longitude,
            #'google_places': google_places_images,
            #'search_results': recent_search_results,
        })

    def get_smithsonian_images(self, request, location_id, *args, **kwargs):
        """
        Returns the Smithsonian images for a location.
        """
        # Get the location
        try:
            location : Location = Location.objects.get(id=location_id)
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(settings.SMITHSONIAN_API_KEY)

        # Get historic images from the Smithsonian's API
        smithsonian_images = smithsonian_gateway.get_data(location.name)

        return render(request, 'dashboard/pages/location/smithsonian.html', {
            'smithsonian': smithsonian_images,
        })
