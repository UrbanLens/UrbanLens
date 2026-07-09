from __future__ import annotations

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User, dispatch_uid="profile_create_user_profile")
def create_user_profile(sender: type[User], instance: User, created: bool, **kwargs) -> None:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.email_normalization import normalize_email

    normalized = normalize_email(instance.email) if instance.email else ""

    if created:
        from urbanlens.dashboard.services.site_admin import promote_first_user_if_needed

        Profile.objects.get_or_create(
            user=instance,
            defaults={"primary_email_normalized": normalized, "profile_setup_complete": False},
        )
        promote_first_user_if_needed(instance)
    else:
        Profile.objects.filter(user=instance).exclude(primary_email_normalized=normalized).update(primary_email_normalized=normalized)
