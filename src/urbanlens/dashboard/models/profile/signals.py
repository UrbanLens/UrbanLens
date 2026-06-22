from __future__ import annotations

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User, dispatch_uid="profile_create_user_profile")
def create_user_profile(sender, instance: User, created: bool, **kwargs) -> None:
    if created:
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.site_admin import promote_first_user_if_needed

        Profile.objects.get_or_create(user=instance)
        promote_first_user_if_needed(instance)
