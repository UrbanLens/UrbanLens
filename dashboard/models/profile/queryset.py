from django.db import models

class ProfileManager(models.Manager):
    def get_all_profiles(self):
        return super().get_queryset().all()
