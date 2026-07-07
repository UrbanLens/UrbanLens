"""Subscription role models and feature access helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib.auth.models import AnonymousUser, User
from django.db.models import CASCADE, CharField, DateTimeField, ForeignKey, Q, TextChoices, UniqueConstraint
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


class SiteFeature(TextChoices):
    """Feature flags that can be unlocked by subscription roles."""

    AI = "ai", "AI features"
    PLACES = "places", "Places layer (Google Places landmarks)"
    SEARCH = "search", "Web search engines"


class SubscriptionRole(abstract.DashboardModel):
    """Extensible role definition that grants a set of site features."""

    slug = CharField(max_length=50, unique=True, db_index=True)
    name = CharField(max_length=100)
    description = CharField(max_length=255, blank=True)
    features = CharField(max_length=500, blank=True, help_text="Comma-separated SiteFeature values.")

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["name"]

    # Canonical features every VIP role must include.  Add new SiteFeature values here
    # when they should be automatically granted to VIPs; ensure_defaults will merge them
    # into existing rows without removing any admin-configured extras.
    _VIP_CANONICAL_FEATURES: frozenset[str] = frozenset({SiteFeature.AI, SiteFeature.PLACES, SiteFeature.SEARCH})

    @classmethod
    def ensure_defaults(cls) -> None:
        """Create or update built-in roles, merging in any newly-added canonical features."""
        role, created = cls.objects.get_or_create(
            slug="vip",
            defaults={
                "name": "VIP",
                "description": "Grants access to VIP-only features.",
                "features": ",".join(sorted(cls._VIP_CANONICAL_FEATURES)),
            },
        )
        if not created:
            existing = role.feature_set
            missing = cls._VIP_CANONICAL_FEATURES - existing
            if missing:
                merged = ",".join(sorted(existing | missing))
                cls.objects.filter(pk=role.pk).update(features=merged)

    @property
    def feature_set(self) -> set[str]:
        return {feature.strip() for feature in (self.features or "").split(",") if feature.strip()}

    def grants(self, feature: SiteFeature | str) -> bool:
        return str(feature) in self.feature_set

    def __str__(self) -> str:
        return self.name


class UserSubscription(abstract.DashboardModel):
    """Subscription role granted to a user by a site administrator."""

    user = ForeignKey(User, on_delete=CASCADE, related_name="subscriptions")
    role = ForeignKey(SubscriptionRole, on_delete=CASCADE, related_name="user_subscriptions")
    granted_by = ForeignKey(User, on_delete=CASCADE, related_name="granted_subscriptions")
    expires_at = DateTimeField(null=True, blank=True)
    revoked_at = DateTimeField(null=True, blank=True)

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]
        constraints = [
            UniqueConstraint(
                fields=["user", "role"],
                condition=Q(revoked_at__isnull=True),
                name="unique_active_user_subscription_role",
            ),
        ]

    @property
    def is_indefinite(self) -> bool:
        return self.expires_at is None

    def is_active(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > timezone.now())

    def set_duration_months(self, months: int | None) -> None:
        self.expires_at = None if months is None else timezone.now() + timedelta(days=months * 30)

    def revoke(self) -> None:
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated"])

    def __str__(self) -> str:
        return f"{self.user} → {self.role}"


class PendingSubscriptionGrant(abstract.DashboardModel):
    """Subscription grant attached to an invite for a user who has not joined yet."""

    invitation = ForeignKey("dashboard.FriendInvitation", on_delete=CASCADE, related_name="pending_subscription_grants")
    role = ForeignKey(SubscriptionRole, on_delete=CASCADE, related_name="pending_grants")
    granted_by = ForeignKey(User, on_delete=CASCADE, related_name="pending_subscription_grants")
    duration_months = CharField(max_length=20, blank=True, help_text="Blank means indefinite.")

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]

    def duration_as_int(self) -> int | None:
        if not self.duration_months:
            return None
        return int(self.duration_months)


def user_has_feature(user: AbstractBaseUser | AnonymousUser, feature: SiteFeature | str) -> bool:
    """Return whether the user has an active role granting the feature.

    Anonymous users never have subscription-backed features. Site admins are
    treated as having every subscription tier and feature. Keeping this guard
    in the helper lets callers use ``request.user`` directly without duplicating
    authentication/type checks throughout controllers and context processors.
    """
    if not isinstance(user, User) or not user.is_authenticated:
        return False
    if user.has_perm("dashboard.view_site_admin"):
        return True
    now = timezone.now()
    subscriptions = (
        UserSubscription.objects.filter(
            user=user,
            revoked_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .select_related("role")
    )
    return any(subscription.role.grants(feature) for subscription in subscriptions)


def grant_subscription(user: User, role: SubscriptionRole, granted_by: User, months: int | None) -> UserSubscription:
    """Create or update an active grant for a user and role."""
    subscription = UserSubscription.objects.filter(user=user, role=role, revoked_at__isnull=True).first()
    if subscription is None:
        subscription = UserSubscription(user=user, role=role, granted_by=granted_by)
    subscription.set_duration_months(months)
    subscription.granted_by = granted_by
    subscription.revoked_at = None
    subscription.save()
    return subscription
