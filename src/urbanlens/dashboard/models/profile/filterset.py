from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.profile.model import Profile


class ProfileFilter(django_filters.FilterSet):
    class Meta:
        model = Profile
        fields = ["user", "icon", "categories", "priority", "last_visited"]
