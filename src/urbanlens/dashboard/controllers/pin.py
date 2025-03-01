"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    pin.py                                                                                        *
*        - Path:    /dashboard/controllers/pin.py                                                                 *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-01-01                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from datetime import datetime
import logging

from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from requests.exceptions import HTTPError

from rest_framework.viewsets import GenericViewSet
from rest_framework.decorators import action
from django.http import JsonResponse
from rest_framework.exceptions import ValidationError

from urbanlens.settings.app import settings
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.smithsonian import SmithsonianGateway
from urbanlens.dashboard.services.google.search import GoogleCustomSearchGateway
from urbanlens.dashboard.services.google.maps import GoogleMapsGateway
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile

logger = logging.getLogger(__name__)

class PinController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the pin page
    """
    def view(self, request, *args, **kwargs):
        """
        View the pin page
        """
        pin = Pin.objects.get(id=kwargs['pin_id'])

        return render(request, 'dashboard/pages/pin/index.html', { 'pin': pin, 'google_maps_api_key': settings.google_maps_api_key })
    
    def test_ai(self, request, *args, **kwargs):
        """
        Test the AI. TODO Temporary function that can be deleted at any time with no side effects.
        """
        from urbanlens.dashboard.models.profile import Profile
        profile = Profile.objects.get(pk=1)
        pin, created = Pin.objects.get_nearby_or_create(latitude=43.0423439, longitude=-76.1501928, profile=profile, defaults={
            'name': 'Syracuse Central High School',
            'description': '',
        })
        logger.critical('Location: %s', pin)
        return JsonResponse({'pin': pin.to_json()})

        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway
        instructions = "" +\
            "Look at the following information about a location and determine what category it belongs in. Example categories are:" +\
            "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, Firehouse, Fire Tower, " +\
            "Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, Library, Lighthouse, Mall, Mansion, Military Base, " +\
            "Monument, Police Station, Power Plant, Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel" +\
            "If the Pin does not fit into any of these categories, provide a new category that is broad enough to include a variety " +\
            "of similar urbex locations. Do not answer with the name of the location; always answer with a category, like this: <ANSWER>Factory</ANSWER>."

        gateway = CloudflareGateway(instructions=instructions)
        response = gateway.send_prompt('address: 312 Western Ave, Guilderland, NY 12084, USA, name: Master Cleaners')

        return JsonResponse({'response': response})

    def init_map(self, request, *args, **kwargs):
        map_data = self.get_map_data()

        # Preprocess data into strings
        for pin in map_data:
            if 'description' in pin and pin['description'] is None:
                pin['description'] = ''

            # Turn arrays into csv
            if 'tags' in pin and pin['tags']:
                pin['tags'] = ', '.join(pin['tags'])
            else:
                pin['tags'] = ''
            if 'categories' in pin and pin['categories']:
                pin['categories'] = ', '.join(pin['categories'])
            else:
                pin['categories'] = ''

            # Last visited = None => Never
            if not pin['last_visited'] or pin['last_visited'] == 'never':
                pin['last_visited'] = 'Never'
            else:
                try:
                    # Dates look like this: 2023-01-02T00:00:00+00:00
                    pin['last_visited'] = datetime.strptime(pin['last_visited'], '%Y-%m-%dT%H:%M:%S%z').strftime('%Y-%m-%d')
                except ValueError:
                    logger.warning('Unable to parse date: %s', pin['last_visited'])

            if pin['status']:
                pin['status'] = pin['status'].replace('_', ' ').capitalize()

        return render(request, 'dashboard/pages/map/data.html', {'map_data': map_data})

    def get_map_data(self):
        map_data = Pin.objects.all()
        if not map_data:
            # Default map data
            map_data = [{'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Pin', 'description': 'No pins saved yet.'}]
        else:
            map_data = [pin.to_json() for pin in map_data]

        return map_data

    def get_smithsonian_images(self, request, pin_id, *args, **kwargs):
        """
        Returns the Smithsonian images for a pin.
        """
        # Get the pin
        try:
            pin : Pin = Pin.objects.get(id=pin_id)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(settings.smithsonian_api_key)

        # Get historic images from the Smithsonian's API
        smithsonian_images = smithsonian_gateway.get_data(pin.name)

        return render(request, 'dashboard/pages/pin/smithsonian.html', {
            'images': smithsonian_images,
        })

    def web_search(self, request, pin_id, *args, **kwargs):
        """
        Returns the web search results for a pin.
        """
        # Get the pin
        try:
            pin : Pin = Pin.objects.get(id=pin_id)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleCustomSearchGateway with the API key
        try:
            google_gateway = GoogleCustomSearchGateway()

            # Get web search results from the Google Custom Search API
            query = [
                pin.address_extended,
                [
                    pin.address_basic,
                    pin.city
                ],
                [
                    pin.address_basic,
                    pin.county
                ],
                [
                    pin.address_basic,
                    pin.state
                ],
                f'{pin.latitude}, {pin.longitude}'
            ]

            if pin.name and pin.address_basic != pin.name:
                query.append([
                    pin.name,
                    pin.city
                ])

            place_name = pin.place_name
            if place_name and place_name != pin.address_basic and place_name != pin.name:
                query.append(place_name)

            search_results = google_gateway.search(query)
        except HTTPError as e:
            logger.error('Unable to contact Google Search API. Is the API Key valid? Exception ---> %s', e)
            return HttpResponse("Unable to search. This is unlikely to be resolved by multiple requests.", status=500)

        return render(request, 'dashboard/pages/pin/web_search.html', { 'search_results': search_results })

    def satellite_view_google_image(self, request, *args, **kwargs):
        """
        Returns the satellite view image for a pin.
        """
        try:
            pin = Pin.objects.get(id=kwargs['pin_id'])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

        # Get the satellite view image from the Google Maps API
        satellite_image = google_maps_gateway.get_satellite_view(pin.latitude, pin.longitude)

        return HttpResponse(satellite_image, content_type="image/jpeg")

    def street_view(self, request, *args, **kwargs):
        """
        Returns the street view image for a pin.
        """
        try:
            pin = Pin.objects.get(id=kwargs['pin_id'])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

        # Get the street view image from the Google Maps API
        street_view_image = google_maps_gateway.get_street_view(pin.latitude, pin.longitude)

        return HttpResponse(street_view_image, content_type="image/jpeg")

    @action(detail=True, methods=['get'])
    def import_form(self, request, *args, **kwargs):
        """
        View the import pins form
        """
        return render(request, 'dashboard/pages/pin/import/csv.html', { 'form': UploadDataFile() })

    @action(detail=True, methods=['post'])
    def upload_takeout(self, request, *args, **kwargs):
        """
        Upload a Google Takeout file
        """
        logger.critical('Uploading a takeout file')
        try:
            form = UploadDataFile(request.POST, request.FILES)
            if form.is_valid():
                datafile = form.cleaned_data['file']

                # Instantiate the GoogleMapsGateway with the API key
                google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

                # Get the file extension
                pins = google_maps_gateway.import_pins_from_file(datafile, request.user.profile)

                return JsonResponse({'pins': [pin.to_json() for pin in pins]})
            else:
                return JsonResponse({'error': 'Invalid form'}, status=400)

        except ValidationError as e:
            return JsonResponse({'error': str(e)}, status=400)

    def weather_forecast(self, request, pin_id, *args, **kwargs):
        """
        Returns the weather forecast for a pin.
        """
        # Get the pin
        try:
            pin : Pin = Pin.objects.get(id=pin_id)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        from urbanlens.dashboard.services.openweather.gateway import WeatherForecastGateway

        # Instantiate the WeatherForecastGateway with the API key
        weather_forecast_gateway = WeatherForecastGateway()

        # Get the weather forecast from the OpenWeather API
        weather_forecast = weather_forecast_gateway.get_weather_forecast(pin.latitude, pin.longitude)

        logger.debug('forecast_data: %s', weather_forecast)

        return render(request, 'dashboard/pages/pin/weather.html', { 'forecast': weather_forecast })