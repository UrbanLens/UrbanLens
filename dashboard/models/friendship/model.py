from django.db import models
from django.contrib.auth.models import User
from dashboard.models.abstract.model import Model

class Friendship(Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user')
    friend = models.ForeignKey(User, on_delete=models.CASCADE, related_name='friend')

    class Meta(Model.Meta):
        db_table = 'dashboard_friendships'
        unique_together = ('user', 'friend')
