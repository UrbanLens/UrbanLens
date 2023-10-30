from __future__ import annotations
from django.db.models import ImageField, CASCADE
from djangofoundry.models import ForeignKey
from dashboard.models import abstract

class Image(abstract.Model):
    """
    Records image data.
    """
    image = ImageField()
    location = ForeignKey(
        'dashboard.Location', 
        on_delete=CASCADE, 
        related_name='images'
    )

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_images'
        get_latest_by = 'updated'
