from django.shortcuts import render, redirect
from django.http import HttpResponse, HttpResponseRedirect
from dashboard.models.locations.model import Location
from dashboard.models.categories.model import Category
from dashboard.models.images.model import Image
from dashboard.models.tags.model import Tag
from django.urls import reverse

def view_map(request):
    locations = Location.objects.all()
    return render(request, 'dashboard/map.html', {'locations': locations})

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
        # Create a new location based on the form data
        name = request.POST.get('name')
        description = request.POST.get('description')
        latitude = request.POST.get('latitude')
        longitude = request.POST.get('longitude')
        tags = request.POST.get('tags').split(',')
        icon = request.FILES.get('icon', None)
        location = Location.objects.create(name=name, description=description, latitude=latitude, longitude=longitude, icon=icon)
        for tag_name in tags:
            tag, created = Tag.objects.get_or_create(name=tag_name)
            location.tags.add(tag)
        return HttpResponse(status=200)
    else:
        # Render the add form
        return render(request, 'dashboard/add_location.html', {'hx': True})

def search_pins(request):
    query = request.GET.get('q')
    locations = Location.objects.filter(name__icontains=query)
    return render(request, 'dashboard/map.html', {'locations': locations})

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
