from django.contrib.auth.models import User
from django.db.models import CASCADE, Index
from djangofoundry.models import OneToOneField, TextField, DateField, CharField, DateTimeField
from dashboard.models.abstract.model import Model
from dashboard.models.profile.queryset import Manager

from django.db.models import ImageField

class Profile(Model):
    user = OneToOneField(User, on_delete=CASCADE)
    avatar = ImageField(upload_to='avatars/', null=True, blank=True)

    objects = Manager()

    class Meta(Model.Meta):
        db_table = 'dashboard_profiles'

        indexes = [
            Index(fields=['user']),
        ]
