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
from datetime import datetime
import json
import logging

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

from django.shortcuts import render
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
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

        # Get historic images from the Smithsonian's API
        smithsonian_images = smithsonian_gateway.get_data(location.name)
        google_places_images = google_places_gateway.get_data(location.latitude, location.longitude, radius=1000)

        # Fetch the most recent search results for the location
        recent_search_results = google_places_gateway.get_recent_search_results(location.name)

        return render(request, 'dashboard/pages/location/index.html', {
            'location': location,
            'smithsonian_images': smithsonian_images,
            'google_places_images': google_places_images,
            'recent_search_results': recent_search_results,
        })
