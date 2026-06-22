from __future__ import annotations

import django_filters

from urbanlens.dashboard.models.images.model import Image


class ImageFilterSet(django_filters.FilterSet):
    class Meta:
        model = Image
        fields = ["image", "pin"]
