from django.db.models import CharField, ForeignKey, CASCADE
from dashboard.models import abstract
from dashboard.models.locations.model import Location
from dashboard.models.profile.model import Profile

class Comment(abstract.Model):
    """
    Records comment data.
    """
    text = CharField(max_length=500)
    location = ForeignKey(Location, on_delete=CASCADE, related_name='comments')
    profile = ForeignKey(Profile, on_delete=CASCADE, related_name='comments')

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_comments'
        get_latest_by = 'updated'
