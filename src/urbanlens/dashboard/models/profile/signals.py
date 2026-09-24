from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User, dispatch_uid="profile_create_user_profile")
def create_user_profile(sender: type[User], instance: User, created: bool, **kwargs) -> None:
    from django.db.models import Case, F, Q, Value, When

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email
    from urbanlens.dashboard.services.auth.username import normalize_username_key

    normalized = normalize_email(instance.email) if instance.email else ""
    username_key = normalize_username_key(instance.username or "")

    if created:
        from urbanlens.dashboard.services.admin.site_admin import promote_first_user_if_needed

        Profile.objects.get_or_create(
            user=instance,
            defaults={"primary_email_normalized": normalized, "username_key": username_key, "profile_setup_complete": False},
        )
        promote_first_user_if_needed(instance)
    else:
        stale = ~Q(primary_email_normalized=normalized) | ~Q(username_key=username_key)
        # A proof of the old primary is not a proof of the new one, and verified_primary_email is unique, so
        # keeping it would also hold the old address against whoever proves it next.
        Profile.objects.filter(user=instance).filter(stale).update(
            primary_email_normalized=normalized,
            username_key=username_key,
            verified_primary_email=Case(When(verified_primary_email=normalized, then=F("verified_primary_email")), default=Value("")),
        )


@receiver(user_logged_in, dispatch_uid="profile_warm_saved_filter_cache_on_login")
def warm_saved_filter_cache_on_login(sender: type[User], request, user: User, **kwargs) -> None:
    """Prewarm the map toolbar's saved-filter cache after login."""
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import warm_saved_filter_cache

    profile = Profile.objects.filter(user=user).first()
    if profile is not None and profile.saved_filters.exists():
        safely_enqueue_task(warm_saved_filter_cache, profile.pk)
