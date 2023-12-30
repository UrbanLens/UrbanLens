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
*        Path:    /dashboard/controllers/MapController.py                                                              *
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


def view_map(request):
    locations = Location.objects.all()
    reviews = Review.objects.filter(location__in=locations)
    form = ReviewForm()
    return render(request, 'dashboard/pages/map/index.html', {'locations': locations, 'reviews': reviews, 'form': form})

def edit_pin(request, location_id):
    location = Location.objects.get(id=location_id)
    if request.method == 'POST':
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
    else:
        # Render the edit form
        categories = Category.objects.all()
        return render(request, 'dashboard/edit_location.html', {'location': location, 'categories': categories})

def add_pin(request):
    if request.method == 'POST':
        try:
            # Create a new location based on the form data
            name = request.POST.get('name')
            description = request.POST.get('description')
            latitude = request.POST.get('latitude')
            longitude = request.POST.get('longitude')
            address = request.POST.get('address')
            tags = request.POST.get('tags')
            if tags is not None:
                tags = tags.split(',')
            else:
                tags = []
            icon = request.FILES.get('icon', None)

            if not latitude or not longitude:
                if not address:
                    return HttpResponse("Error: No address or lat/lon provided.", status=400)
                
                # Convert address into lat/lng
                (latitude, longitude) = get_location_by_address(address)
                if not latitude or not longitude:
                    return HttpResponse("Error: Unable to convert address to lat/lng.", status=400)

            location = Location.objects.create(name=name, description=description, latitude=latitude, longitude=longitude, icon=icon)
            for tag_name in tags:
                tag, created = Tag.objects.get_or_create(name=tag_name)
                location.tags.add(tag)
            return HttpResponse(status=200)
        except Exception as e:
            return HttpResponse(f"Error: {str(e)}", status=400)
    else:
        # Render the add form
        return render(request, 'dashboard/pages/map/add_location.html')

def search_pins(request):
    query = request.GET.get('q')
    locations = Location.objects.filter(name__icontains=query)
    return render(request, 'dashboard/pages/map/map.html', {'locations': locations})

def upload_image(request, location_id):
    if request.method == 'POST':
        image = request.FILES.get('image')
        location = Location.objects.get(id=location_id)
        Image.objects.create(image=image, location=location)
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=405)

def change_category(request, location_id):
    if request.method == 'POST':
        category_id = request.POST.get('category')
        location = Location.objects.get(id=location_id)
        location.change_category(category_id)
        return HttpResponseRedirect(reverse('view_map'))
    else:
        return HttpResponse(status=405)

@login_required
def advanced_search(request):
    if request.method == 'POST':
        form = AdvancedSearchForm(request.POST)
        if form.is_valid():
            locations = Location.objects.filter_by_criteria(form.cleaned_data)
            return render(request, 'dashboard/view_map.html', {'locations': locations})
    else:
        form = AdvancedSearchForm()
    return render(request, 'dashboard/advanced_search.html', {'form': form})

@login_required
def add_review(request, location_id):
    if request.method == 'POST':
        form = ReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.user = request.user
            review.location = Location.objects.get(id=location_id)
            review.save()
            return HttpResponse(status=200)
    else:
        return HttpResponse(status=405)

def get_map_data():
    map_data = Location.objects.values('latitude', 'longitude', 'name', 'description')
    if not map_data:
        # Default map data
        map_data = [{'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Location', 'description': 'No pins saved yet.'}]
    return map_data

def init_map(request):
    map_data = get_map_data()
    return HttpResponse(json.dumps(list(map_data)), content_type='application/json')

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
