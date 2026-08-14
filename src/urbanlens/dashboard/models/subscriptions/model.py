"""Subscription role models and feature access helpers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import CASCADE, BooleanField, CharField, DateTimeField, ForeignKey, IntegerField, Q, TextChoices, UniqueConstraint
from django.utils import timezone
from django.utils.text import slugify

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.subscriptions.queryset import PendingSubscriptionGrantManager, SubscriptionRoleManager, UserSubscriptionManager

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


class SiteFeature(TextChoices):
    """Feature flags that can be unlocked by subscription roles."""

    AI = "ai", "AI features"
    # Deliberately separate from AI: vision calls on every upload cost far more
    # than the text features AI covers, so admins grant this tier explicitly
    # (it is not part of the VIP canonical set).
    AI_PHOTO_PROCESSING = "ai_photo_processing", "AI photo processing"
    PLACES = "places", "Places layer (Google Places landmarks)"
    SEARCH = "search", "Web search engines"
    # Deliberately separate from the VIP canonical set: video files are far
    # larger than photos, so admins grant this tier explicitly to manage
    # storage cost rather than bundling it into every VIP subscription.
    VIDEO_UPLOADS = "video_uploads", "Video uploads"
    # Documents are converted to PDF and OCR'd on upload (see services.media.documents),
    # which costs meaningfully more CPU than a photo upload - kept as its own
    # grant for the same reason as VIDEO_UPLOADS.
    DOCUMENT_UPLOADS = "document_uploads", "Document uploads"
    # Nearby-facility/feature research tabs on the pin detail page (e.g. EPA's
    # nearby-regulated-facilities list) - separate from each plugin's own
    # unconditional "data about this exact pin" card, which everyone gets.
    NEARBY_RESEARCH = "nearby_research", "Nearby research data"
    # Owner identity/contact details UrbanLens looked up *for* the user from
    # county assessor records (via REData) - the paid data feed, and the part
    # of a property record that names a private individual. Deliberately scoped
    # to automatically-sourced records only: a user's own private PinOwner
    # notes, and WikiOwner rows the community typed in themselves, are the
    # users' own contributions and stay visible to everyone (see
    # ``services.property.owner_access``).
    PROPERTY_OWNERS = "property_owners", "Official property owner names & contact info"
    # Gates access to features still under active development, but stable enough
    # for general (VIP) use - a single flag reused across every in-progress
    # feature, rather than one SiteFeature per beta, so a feature can graduate
    # out of beta by just removing its gate instead of touching this enum.
    BETA_FEATURES = "beta_features", "Beta features"
    # Earlier-stage than BETA_FEATURES: features not yet ready for general use.
    # Deliberately excluded from the built-in VIP role's seeded features and from
    # any default SiteSettings.default_features, so user_has_feature() only grants this to
    # site admins (via its own admin check) unless a site admin explicitly
    # grants it to a role or as a site-wide default.
    ALPHA_FEATURES = "alpha_features", "Alpha features"


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

    # --- Paid subscriptions (Stripe) ---

    monthly_price_cents = IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Fixed monthly price in cents, e.g. 500 for $5.00. Blank means not offered at a fixed price.",
    )
    pay_what_you_want = BooleanField(
        default=False,
        help_text="Let users choose their own monthly pledge instead of (or as well as) the fixed price above.",
    )
    pwyw_dynamic_threshold = BooleanField(
        default=False,
        help_text="Requires pay_what_you_want. Grants this role only in months the user's pledge meets or exceeds "
        "the site's current cost-per-user (services.admin.cost_tracking.cost_per_user), recalculated at each "
        "successful charge - not a fixed minimum.",
    )
    pwyw_minimum_cents = IntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Static minimum pledge (cents) required to hold this role when pay_what_you_want is on and pwyw_dynamic_threshold is off. Blank or 0 means any pledge grants it.",
    )
    stripe_product_id = CharField(
        max_length=255,
        blank=True,
        help_text="Stripe Product id for this role, created lazily on first checkout. Prices are generated inline per checkout/pledge instead of being pre-created, so editing monthly_price_cents needs no Stripe sync.",
    )

    objects = SubscriptionRoleManager()

    class Meta(abstract.DashboardModel.Meta):
        ordering = ["name"]

    @classmethod
    def unique_slug(cls, name: str) -> str:
        """Derive a unique slug from an admin-entered role name.

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
        return [label for value, label in SiteFeature.choices if value in feature_set]

    def grants(self, feature: SiteFeature | str) -> bool:
        return str(feature) in self.feature_set

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.pwyw_dynamic_threshold and not self.pay_what_you_want:
            errors["pwyw_dynamic_threshold"] = "Requires pay_what_you_want to be enabled."
        if self.pwyw_dynamic_threshold and self.pwyw_minimum_cents:
            errors["pwyw_minimum_cents"] = "Cannot be set together with pwyw_dynamic_threshold - the dynamic cost-per-user figure is used instead."
        if errors:
            raise ValidationError(errors)

    @property
    def monthly_price_dollars(self) -> Decimal | None:
        """The fixed monthly price in dollars, or None when not offered at a fixed price."""
        if self.monthly_price_cents is None:
            return None
        return Decimal(self.monthly_price_cents) / 100

    @property
    def pwyw_minimum_dollars(self) -> Decimal | None:
        """The static PWYW minimum in dollars, or None when unset/not applicable."""
        if not self.pwyw_minimum_cents:
            return None
        return Decimal(self.pwyw_minimum_cents) / 100

    @property
    def is_purchasable(self) -> bool:
        """Whether users can pay for this role at all (fixed price or pay-what-you-want)."""
        return self.monthly_price_cents is not None or self.pay_what_you_want

    def __str__(self) -> str:
        return self.name


class UserSubscription(abstract.DashboardModel):
    """Subscription role granted to a user by a site administrator."""

    expires_at = DateTimeField(null=True, blank=True)
    revoked_at = DateTimeField(null=True, blank=True)

    user = ForeignKey(User, on_delete=CASCADE, related_name="subscriptions")
    role = ForeignKey(SubscriptionRole, on_delete=CASCADE, related_name="user_subscriptions")
    granted_by = ForeignKey(User, on_delete=CASCADE, related_name="granted_subscriptions")

    if TYPE_CHECKING:
        user_id: int
        role_id: int
        granted_by_id: int

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


def _paid_subscription_roles(user: User) -> list[SubscriptionRole]:
    """Roles the user currently holds via a paid (Stripe-backed) subscription.

    Only counts ``RoleSubscription`` rows that are active/trialing *and* have
    cleared their role's pay-what-you-want threshold (see
    ``services.billing.pricing.role_pwyw_threshold_cents`` - re-evaluated at
    each successful Stripe charge, so a dynamic-threshold role can silently
    stop granting access without the row itself changing status).
    """
    from urbanlens.dashboard.models.billing import RoleSubscription

    paid = RoleSubscription.objects.granting_access_for(user).select_related("role")
    return [subscription.role for subscription in paid]


def user_has_feature(user: AbstractBaseUser | AnonymousUser, feature: SiteFeature | str) -> bool:
    """Return whether the user has the feature, via the site default or an active role.

    Anonymous users never have subscription-backed features. Site admins are
    treated as having every subscription tier and feature. Authenticated users
    also get whatever ``SiteSettings.default_features`` grants everyone, even
    with no subscription at all. Keeping this guard in the helper lets callers
    use ``request.user`` directly without duplicating authentication/type checks
    throughout controllers and context processors.
    """
    if not isinstance(user, User) or not user.is_authenticated:
        return False
    if user.has_perm("dashboard.view_site_admin"):
        return True
    from urbanlens.dashboard.models.site_settings import SiteSettings

    if SiteSettings.get_current().grants(feature):
        return True
    subscriptions = UserSubscription.objects.active_for(user).select_related("role")
    if any(subscription.role.grants(feature) for subscription in subscriptions):
        return True
    return any(role.grants(feature) for role in _paid_subscription_roles(user))


def active_subscription_roles(user: AbstractBaseUser | AnonymousUser) -> list[SubscriptionRole]:
    """Return the subscription roles the user currently holds, admin-granted or paid.

    Args:
        user: The user to look up; anonymous users hold no roles.

    Returns:
        The roles of the user's active (unrevoked, unexpired) admin grants, plus any
        role currently held via a paid subscription that's cleared its access threshold.
        Deduplicated by role.
    """
    if not isinstance(user, User) or not user.is_authenticated:
        return []
    subscriptions = UserSubscription.objects.active_for(user).select_related("role")
    roles = {subscription.role_id: subscription.role for subscription in subscriptions}
    for role in _paid_subscription_roles(user):
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
