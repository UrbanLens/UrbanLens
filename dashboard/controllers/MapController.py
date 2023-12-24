from django.shortcuts import render
from django.http import HttpResponse
from dashboard.models import Pin

def view_map(request):
    pins = Pin.objects.all()
    return render(request, 'dashboard/map.html', {'pins': pins})

def edit_pin(request, pin_id):
    pin = Pin.objects.get(id=pin_id)
    if request.method == 'POST':
        # Update the pin based on the form data
        # Redirect to the map view
    else:
        # Render the edit form
        return render(request, 'dashboard/edit_pin.html', {'pin': pin})

def add_pin(request):
    if request.method == 'POST':
        # Create a new pin based on the form data
        # Redirect to the map view
    else:
        # Render the add form
        return render(request, 'dashboard/add_pin.html')

def search_pins(request):
    query = request.GET.get('q')
    pins = Pin.objects.filter(name__icontains=query)
    return render(request, 'dashboard/map.html', {'pins': pins})
