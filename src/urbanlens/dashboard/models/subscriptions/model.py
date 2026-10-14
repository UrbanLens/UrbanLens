"""Subscription role models and feature access helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib.auth.models import AnonymousUser, User
from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, ForeignKey, IntegerField, Q, TextChoices, UniqueConstraint
from django.utils import timezone
from django.utils.text import slugify

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.subscriptions.queryset import PendingSubscriptionGrantManager, SubscriptionRoleManager, UserSubscriptionManager

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


class SiteFeature(TextChoices):
    """Feature flags that can be unlocked by subscription roles."""

    PLACES = "places", "Places layer (Google Places landmarks)"
    SEARCH = "search", "Web search engines"
    # larger than photos, so admins grant this tier explicitly to manage
    # storage cost rather than bundling it into every VIP subscription.
    VIDEO_UPLOADS = "video_uploads", "Video uploads"
    # Documents are converted to PDF and OCR'd on upload (see services.media.documents),
    # which costs meaningfully more CPU than a photo upload - kept as its own
    # grant for the same reason as VIDEO_UPLOADS.
    DOCUMENT_UPLOADS = "document_uploads", "Document uploads"
    # Nearby-facility/feature research tabs on the Private Pin page - separate from each plugin's
    # own unconditional "data about this exact pin" card, which everyone gets.
    # This flag names the concept, not every panel that could plausibly use it - check each
    # PanelSource.required_feature, don't assume from this comment.
    NEARBY_RESEARCH = "nearby_research", "Nearby research data"
    # Full incident history for a pin's block (up to 25 years back) - a deeper pull than the
    # free "Reported Incidents" card everyone gets (last 3 years, top few rows only).
    # Kept as its own flag rather than folded into NEARBY_RESEARCH: unlike a nearby-facility list, a
    # block's multi-year crime-report history is sensitive enough that a site may want to manage
    INCIDENT_HISTORY = "incident_history", "Historical incident data"
    # Owner identity/contact details UrbanLens looked up *for* the user from county assessor records
    # - the paid data feed, and the part of a property record that names a private
    # individual.
    PROPERTY_OWNERS = "property_owners", "Official property owner names & contact info"
    # Automatic tag/category assignment from an external label-suggestion service: it matches a place against
    # the user's own taxonomy, so the cost profile and the grant decision are
    # different.
    AUTO_TAGGING = "auto_tagging", "Automatic tagging of pins"
    # Gates access to features still under active development, but stable enough for general (VIP)
    # use - a single flag reused across every in-progress feature, rather than one SiteFeature per
    # beta, so a feature can graduate out of beta by just removing its gate instead of touching this
    # enum.
    BETA_FEATURES = "beta_features", "Beta features"
    # Earlier-stage than BETA_FEATURES: features not yet ready for general use.
    ALPHA_FEATURES = "alpha_features", "Alpha features"


def site_feature_choices() -> list[tuple[str, str]]:
    """Every grantable feature: the ``SiteFeature`` members, then those plugins contribute."""
    from urbanlens.dashboard.plugins.points import SITE_FEATURES

    return [*SiteFeature.choices, *((value, label) for value, label in SITE_FEATURES.choices() if value not in SiteFeature.values)]


def site_feature_values() -> frozenset[str]:
    """The value of every grantable feature."""
    return frozenset(value for value, _label in site_feature_choices())


class SubscriptionRole(abstract.DashboardModel):
    """Extensible role definition that grants a set of site features."""

    slug = CharField(max_length=50, unique=True, db_index=True)
    name = CharField(max_length=100)
    description = CharField(max_length=255, blank=True)
    features = CharField(max_length=500, blank=True, help_text="Comma-separated SiteFeature values.")
    # Storage quota (GB) granted to users holding this role. Null means the role
    # grants no quota of its own and the site-wide default applies. When a user
    # holds several active roles, the largest applicable quota wins.
    storage_quota_gb = IntegerField(
        null=True,
        blank=True,
        help_text="Storage quota (GB) for users with this role. Blank uses the site default; 0 means unlimited.",
    )
    # Outbound email caps for this role. Null falls back to the site-wide
    # default; when a user holds several active roles the largest applicable
    # limit wins and 0 means unlimited (see services.security.email_safety).
    email_limit_per_hour = IntegerField(
        null=True,
        blank=True,
        help_text="Max user-triggered emails per hour for this role. Blank uses the site default; 0 means unlimited.",
    )
    email_limit_per_day = IntegerField(
        null=True,
        blank=True,
        help_text="Max user-triggered emails per day for this role. Blank uses the site default; 0 means unlimited.",
    )
    email_limit_per_month = IntegerField(
        null=True,
        blank=True,
        help_text="Max user-triggered emails per 30 days for this role. Blank uses the site default; 0 means unlimited.",
    )

    objects = SubscriptionRoleManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["name"]

    @classmethod
    def unique_slug(cls, name: str) -> str:
        """
        Derive a unique slug from an admin-entered role name.

        Args:
            name: The role name to slugify.

        Returns:
            A slug not currently used by any other role.
        """
        base = slugify(name) or "role"
        candidate = base
        suffix = 2
        while cls.objects.filter(slug=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    @property
    def feature_set(self) -> set[str]:
        return {feature.strip() for feature in (self.features or "").split(",") if feature.strip()}

    @property
    def feature_labels(self) -> list[str]:
        """Human-readable labels for the role's granted features, in declaration order."""
        feature_set = self.feature_set
        return [label for value, label in site_feature_choices() if value in feature_set]

    def grants(self, feature: str) -> bool:
        return str(feature) in self.feature_set

    def __str__(self) -> str:
        return self.name


class UserSubscription(abstract.DashboardModel):
    """Subscription role granted to a user by a site administrator."""

    expires_at = DateTimeField(null=True, blank=True)
    revoked_at = DateTimeField(null=True, blank=True)

    user = ForeignKey(User, on_delete=CASCADE, related_name="subscriptions")
    role = ForeignKey(SubscriptionRole, on_delete=CASCADE, related_name="user_subscriptions")
    granted_by = ForeignKey(User, on_delete=SET_NULL, null=True, blank=True, related_name="granted_subscriptions")
    revoked_by = ForeignKey(User, on_delete=SET_NULL, null=True, blank=True, related_name="revoked_subscriptions")

    if TYPE_CHECKING:
        user_id: int
        role_id: int
        granted_by_id: int | None
        revoked_by_id: int | None

    objects = UserSubscriptionManager()

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

    def revoke(self, *, by: User | None = None) -> None:
        """
        End the grant now.

        Args:
            by: The admin revoking it, if a person is.
        """
        self.revoked_at = timezone.now()
        self.revoked_by = by
        self.save(update_fields=["revoked_at", "revoked_by", "updated"])

    def __str__(self) -> str:
        return f"{self.user} → {self.role}"


class PendingSubscriptionGrant(abstract.DashboardModel):
    """Subscription grant attached to an invite for a user who has not joined yet."""

    invitation = ForeignKey("dashboard.FriendInvitation", on_delete=CASCADE, related_name="pending_subscription_grants")
    role = ForeignKey(SubscriptionRole, on_delete=CASCADE, related_name="pending_grants")
    granted_by = ForeignKey(User, on_delete=CASCADE, related_name="pending_subscription_grants")
    duration_months = CharField(max_length=20, blank=True, help_text="Blank means indefinite.")

    if TYPE_CHECKING:
        invitation_id: int
        role_id: int
        granted_by_id: int

    objects = PendingSubscriptionGrantManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["-created"]

    def duration_as_int(self) -> int | None:
        if not self.duration_months:
            return None
        return int(self.duration_months)


def _additional_subscription_roles(user: User) -> list[SubscriptionRole]:
    """
    Roles the user holds through a means other than a direct admin grant.

    Provided by plugins through the ``additional_subscription_roles`` filter
    (a paid-subscription plugin grants roles whose payment threshold is met).
    Without such a plugin there are no additional roles.

    Args:
        user: The user to look up.

    Returns:
        The extra roles, deduplicated by the caller.
    """
    from urbanlens.dashboard.plugins.points import SUBSCRIPTION_ROLES

    return list(SUBSCRIPTION_ROLES.apply([], user))


def user_has_feature(user: AbstractBaseUser | AnonymousUser, feature: str) -> bool:
    """
    Return whether the user has the feature, via the admin permission, the site default or an active role.
    Anonymous users never have subscription-backed features.
    """
    return str(feature) in user_features(user)


def user_features(user: AbstractBaseUser | AnonymousUser) -> frozenset[str]:
    """
    Every feature the user has, via the admin permission, the site default or an active role.

    Remembered per account rather than per page - see
    :mod:`~urbanlens.dashboard.models.subscriptions.access_state`, which also lists the acts that
    retire it.
    """
    from urbanlens.dashboard.models.subscriptions.access_state import access_state

    state = access_state(user)
    return site_feature_values() if state.admin else state.features


def user_features_from_database(user: User) -> frozenset[str]:
    """
    :func:`user_features`, read from the database instead of the shared cache.

    For a writer inside the transaction that changed a grant: the cache is retired on commit, so until then
    :func:`user_features` can still answer with the standing from before the change.
    """
    from urbanlens.dashboard.models.subscriptions.access_state import compute_access_state

    state = compute_access_state(user)
    return site_feature_values() if state.admin else state.features


def active_subscription_roles(user: AbstractBaseUser | AnonymousUser) -> list[SubscriptionRole]:
    """
    Return the subscription roles the user currently holds, admin-granted or otherwise.

    Args:
        user: The user to look up; anonymous users hold no roles.

    Returns:
        The roles of the user's active (unrevoked, unexpired) admin grants, plus any
        additional roles contributed by plugins (e.g. a paid subscription that's cleared
        its access threshold). Deduplicated by role.
    """
    if not isinstance(user, User) or not user.is_authenticated:
        return []
    subscriptions = UserSubscription.objects.active_for(user).select_related("role")
    roles = {subscription.role_id: subscription.role for subscription in subscriptions}
    for role in _additional_subscription_roles(user):
        roles.setdefault(role.pk, role)
    return list(roles.values())


def grant_subscription(user: User, role: SubscriptionRole, granted_by: User, months: int | None) -> UserSubscription:
    """Create or update an active grant for a user and role."""
    subscription = UserSubscription.objects.not_revoked().filter(user=user, role=role).first()
    if subscription is None:
        subscription = UserSubscription(user=user, role=role, granted_by=granted_by)
    subscription.set_duration_months(months)
    subscription.granted_by = granted_by
    subscription.revoked_at = None
    subscription.save()
    return subscription
