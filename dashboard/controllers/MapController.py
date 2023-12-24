from django.shortcuts import render, redirect
from django.http import HttpResponse, HttpResponseRedirect
from dashboard.models.locations.model import Location
from dashboard.models.categories.model import Category

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
        location.save()
        return JsonResponse({'status': 'success'}, status=200)
    else:
        # Render the edit form
        return render(request, 'dashboard/edit_location.html', {'location': location})

def add_pin(request):
    if request.method == 'POST':
        # Create a new location based on the form data
        name = request.POST.get('name')
        description = request.POST.get('description')
        latitude = request.POST.get('latitude')
        longitude = request.POST.get('longitude')
        Location.objects.create(name=name, description=description, latitude=latitude, longitude=longitude)
        return HttpResponse(status=200)
    else:
        # Render the add form
        return render(request, 'dashboard/add_location.html', {'hx': True})

def search_pins(request):
    query = request.GET.get('q')
    locations = Location.objects.filter(name__icontains=query)
    return render(request, 'dashboard/map.html', {'locations': locations})
