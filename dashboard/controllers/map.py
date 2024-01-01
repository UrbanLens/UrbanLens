"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    MapController.py                                                                                     *
*        Path:    /dashboard/controllers/map.py                                                                        *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import json
import logging

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

from django.shortcuts import render
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required

from dashboard.models.locations.model import Location
from dashboard.models.categories.model import Category
from dashboard.models.images.model import Image
from dashboard.models.tags.model import Tag
from dashboard.forms.review import ReviewForm
from dashboard.models.reviews.model import Review
from dashboard.forms.advanced_search import AdvancedSearchForm


from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin

logger = logging.getLogger(__name__)

class ViewMapView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        locations = Location.objects.all()
        return render(request, 'dashboard/pages/map/index.html', {'locations': locations})

    def post(self, request, *args, **kwargs):
        # Handle the post request here
        return HttpResponse(status=200)

class EditPinView(LoginRequiredMixin, View):
    def post(self, request, location_id, *args, **kwargs):
        location = Location.objects.get(id=location_id)
        # Update the location based on the form data
        location.name = request.POST.get('name')
        location.description = request.POST.get('description')
        location.latitude = request.POST.get('latitude')
        location.longitude = request.POST.get('longitude')
        tags = request.POST.get('tags').split(',')
        for tag_name in tags:
            tag, created = Tag.objects.get_or_create(name=tag_name)
            location.tags.add(tag)
        icon = request.FILES.get('icon', None)
        if icon:
            location.icon = icon
        location.save()
        return HttpResponseRedirect(reverse('view_map'))

    def get(self, request, location_id, *args, **kwargs):
        location = Location.objects.get(id=location_id)
        # Render the edit form
        categories = Category.objects.all()
        return render(request, 'dashboard/edit_location.html', {'location': location, 'categories': categories})


class AddPinView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        # Render the add form
        return render(request, 'dashboard/pages/map/add_location.html')

    def post(self, request, *args, **kwargs):
        logger.info('Adding a new pin!')
        try:
            # Create a new location based on the form data
            name = request.POST.get('name')
            latitude = request.POST.get('latitude')
            longitude = request.POST.get('longitude')
            address = request.POST.get('address', None)
            tags = request.POST.get('tags')
            if tags is not None:
                tags = tags.split(',')
            else:
                tags = []
            icon = request.POST.get('icon', None)

            if not latitude or not longitude:
                if not address:
                    return HttpResponse("Error: No address or lat/lon provided.", status=400)
                
                # Convert address into lat/lng
                (latitude, longitude) = get_location_by_address(address)
                if not latitude or not longitude:
                    return HttpResponse("Error: Unable to convert address to lat/lng.", status=400)

            location = Location.objects.create(name=name, latitude=latitude, longitude=longitude, icon=icon, profile=request.user.profile)
            for tag_name in tags:
                tag, created = Tag.objects.get_or_create(name=tag_name)
                location.tags.add(tag)
            location.save()
            logger.info('New location created: %s', location.name)
            logger.info('Profile is %s', request.user.profile)
            return HttpResponse(status=200)
        except Exception as e:
            raise e from e
            return HttpResponse(f"Error: {str(e)}", status=400)


class SearchPinsView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        query = request.GET.get('q')
        locations = Location.objects.filter(name__icontains=query)
        return render(request, 'dashboard/pages/map/map.html', {'locations': locations})

class UploadImageView(LoginRequiredMixin, View):
    def post(self, request, location_id, *args, **kwargs):
        image = request.FILES.get('image')
        location = Location.objects.get(id=location_id)
        Image.objects.create(image=image, location=location)
        return HttpResponse(status=200)

class ChangeCategoryView(LoginRequiredMixin, View):
    def post(self, request, location_id, *args, **kwargs):
        category_id = request.POST.get('category')
        location = Location.objects.get(id=location_id)
        location.change_category(category_id)
        return HttpResponseRedirect(reverse('view_map'))

class AdvancedSearchView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        form = AdvancedSearchForm(request.POST)
        if form.is_valid():
            locations = Location.objects.filter_by_criteria(form.cleaned_data)
            return render(request, 'dashboard/view_map.html', {'locations': locations})

    def get(self, request, *args, **kwargs):
        form = AdvancedSearchForm()
        return render(request, 'dashboard/advanced_search.html', {'form': form})

class InitMapView(LoginRequiredMixin, View):
    def get_map_data(self):
        map_data = Location.objects.all()
        if not map_data:
            # Default map data
            map_data = [{'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Location', 'description': 'No pins saved yet.'}]
        else:
            map_data = [pin.to_json() for pin in map_data]

        return map_data

    def get(self, request, *args, **kwargs):
        map_data = self.get_map_data()
        return HttpResponse(json.dumps(list(map_data)), content_type='application/json')


@login_required
def get_location_by_address(address):
    try:
        geolocator = Nominatim(user_agent="geoapiExercises")
        location = geolocator.geocode(address)
        if location:
            return (location.latitude, location.longitude)
        
    except GeocoderTimedOut:
        raise Exception("Geocoder service timed out.")
    except GeocoderUnavailable:
        raise Exception("Geocoder service unavailable.")
    return (None, None)
