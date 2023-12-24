from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from dashboard.forms.profile import ProfileForm
from dashboard.models.profile.model import Profile

@login_required
def view_profile(request):
    profile = get_object_or_404(Profile, user=request.user)
    return render(request, 'dashboard/view_profile.html', {'profile': profile})

@login_required
def edit_profile(request):
    profile = get_object_or_404(Profile, user=request.user)
    if request.method == 'POST':
        form = ProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            return redirect('view_profile')
    else:
        form = ProfileForm(instance=profile)
    return render(request, 'dashboard/edit_profile.html', {'form': form})
