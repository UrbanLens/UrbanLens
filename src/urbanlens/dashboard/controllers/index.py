# Generic imports
from __future__ import annotations

from django.shortcuts import redirect
from djangofoundry.controllers import ListController

from urbanlens.dashboard.models.profile import Profile


class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"
    model = Profile

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().get(request, *args, **kwargs)
