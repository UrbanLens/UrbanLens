from django.db.models import ImageField, ForeignKey, CASCADE
from dashboard.models import abstract
from dashboard.models.locations.model import Location

class Image(abstract.Model):
    """
    Records image data.
    """
    image = ImageField(upload_to='images/')
    location = ForeignKey(Location, on_delete=CASCADE, related_name='images')

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_images'
        get_latest_by = 'updated'
