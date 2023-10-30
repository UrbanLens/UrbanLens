from __future__ import annotations
from django.db.models import CASCADE
from djangofoundry.models import CharField, ForeignKey
from dashboard.models import abstract

class Comment(abstract.Model):
    """
    Records comment data.
    """
    text = CharField(max_length=500)

    location = ForeignKey(
        'dashboard.Location', 
        on_delete=CASCADE, 
        related_name='comments'
    )
    profile = ForeignKey(
        'dashboard.Profile', 
        on_delete=CASCADE, 
        related_name='comments'
    )

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_comments'
        get_latest_by = 'updated'
