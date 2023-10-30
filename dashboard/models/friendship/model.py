from __future__ import annotations
from django.db.models import CASCADE
from django.contrib.auth.models import User
from djangofoundry.models import ForeignKey
from dashboard.models.abstract.model import Model

class Friendship(Model):
    user = ForeignKey(
        User, 
        on_delete=CASCADE, 
        related_name='user'
    )
    friend = ForeignKey(
        User, 
        on_delete=CASCADE, 
        related_name='friend'
    )

    class Meta(Model.Meta):
        db_table = 'dashboard_friendships'
        unique_together = ('user', 'friend')
