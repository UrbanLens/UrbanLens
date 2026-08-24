"""Creating, refreshing and removing the integration suite's accounts.

Kept out of the management command so the behaviour that matters - what an
account has to look like for a headless browser to sign in as it, and what
``--purge`` is allowed to select - is testable without a subprocess.

An account provisioned here differs from a registered one in four ways, each
because a browser driving the deployment cannot supply what registration
normally waits for:

- **Active and verified.** Registration leaves ``is_active`` False pending an
  emailed link. Nothing here can click one.
- **Past onboarding.** ``PostLoginRedirectView`` sends a profile that has not
  finished the welcome flow to ``onboarding.welcome``, and one that has not
  finished profile setup to ``profile.edit``. A suite that expects to land on
  the map would fail on its first navigation for a reason that has nothing to do
  with the map.
- **No second factor, and no derived-auth enrolment.** A passkey or TOTP prompt
  is unanswerable headlessly; an ``AccountKdf`` salt makes the login form derive
  its credential in the browser, which works but means the plaintext in the
  manifest is no longer what the form posts.
- **Outbound APIs and AI off, notifications on-site only.** These accounts are
  driven hard and repeatedly. Every provider outside REData bills per call, and
  every email would be addressed to an undeliverable domain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
import secrets
from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope, EmailVerification
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.integration_testing import INTEGRATION_EMAIL_DOMAIN, INTEGRATION_USERNAME_PREFIX

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Roles provisioned when the caller does not name any. Two, because a large
#: share of this application is about what one account can see of another's -
#: sharing, friendships, messages, wiki visibility - and none of that is
#: testable with a single account.
DEFAULT_ROLES: tuple[str, ...] = ("primary", "secondary")

#: Password length in bytes of entropy. These accounts are reachable on a
#: hostname anyone can find, so the password is the only thing protecting them.
_PASSWORD_ENTROPY_BYTES = 24

#: Granted to each account's main key. Every scope, because the suite's job is
#: to exercise the whole surface and a missing scope reads as a broken endpoint.
_FULL_SCOPES: tuple[str, ...] = tuple(scope.value for scope in ApiKeyScope)

#: Granted to each account's second key. Deliberately minimal: a key that is
#: valid but insufficient is the only way to test that scope enforcement is
#: actually wired up, as opposed to being merely declared.
_RESTRICTED_SCOPES: tuple[str, ...] = (ApiKeyScope.PROFILE_READ.value,)


@dataclass(frozen=True)
class ProvisionedAccount:
    """One account, and everything the runner needs to act as it."""

    role: str
    username: str
    email: str
    password: str
    api_key: str | None
    scopes: list[str]
    restricted_api_key: str | None
    restricted_scopes: list[str]
    profile_uuid: str | None
    profile_slug: str | None
    is_staff: bool = False

    def redacted(self) -> dict[str, object]:
        """This account with its secrets replaced, for logging."""
        data = asdict(self)
        for key in ("password", "api_key", "restricted_api_key"):
            if data[key]:
                data[key] = "<redacted>"
        return data


@dataclass
class ProvisionResult:
    """What one provisioning run produced."""

    accounts: list[ProvisionedAccount] = field(default_factory=list)
    created_roles: list[str] = field(default_factory=list)
    refreshed_roles: list[str] = field(default_factory=list)

    def manifest(self, *, site_url: str, environment: str) -> dict[str, object]:
        """The JSON document ``UL_E2E_ACCOUNTS_FILE`` points at.

        Keys are snake_case to match the rest of this codebase; the TypeScript
        loader in ``tests/integration/lib/accounts.ts`` maps them to camelCase
        rather than having Python emit a foreign convention.

        Args:
            site_url: Absolute URL the accounts were provisioned on.
            environment: ``UL_ENVIRONMENT`` of the provisioning instance.

        Returns:
            A JSON-serialisable manifest.
        """
        return {
            "generated_at": timezone.now().isoformat(),
            "site_url": site_url,
            "environment": environment,
            "accounts": [asdict(account) for account in self.accounts],
        }


def username_for(role: str) -> str:
    """The username an account for ``role`` always has.

    Deterministic rather than random, so re-running provisioning refreshes the
    same accounts instead of leaving a new pair behind on every run.

    Args:
        role: Role name, e.g. ``primary``.

    Returns:
        The prefixed username.
    """
    return f"{INTEGRATION_USERNAME_PREFIX}{role}"


def email_for(role: str) -> str:
    """The undeliverable address an account for ``role`` always has.

    Args:
        role: Role name, e.g. ``primary``.

    Returns:
        The address, on the reserved domain.
    """
    return f"{INTEGRATION_USERNAME_PREFIX}{role}@{INTEGRATION_EMAIL_DOMAIN}"


def generate_password() -> str:
    """A fresh, URL-safe password with no characters that need shell quoting."""
    return secrets.token_urlsafe(_PASSWORD_ENTROPY_BYTES)


def integration_users() -> Iterable[User]:
    """Every account this module is allowed to touch.

    Both conventions are required, and staff accounts are excluded outright.
    ``--purge`` deletes what this returns, so the query is the safety boundary:
    widening it is how a manual staging account gets destroyed by a test run.

    Returns:
        A queryset of provisioned integration accounts, ordered by id.
    """
    return (
        User.objects.filter(
            username__startswith=INTEGRATION_USERNAME_PREFIX,
            email__iendswith=f"@{INTEGRATION_EMAIL_DOMAIN}",
            is_staff=False,
            is_superuser=False,
        )
        .select_related("profile")
        .order_by("pk")
    )


@transaction.atomic
def provision_account(role: str, *, password: str, with_api_keys: bool = True, external_apis: bool = False) -> tuple[ProvisionedAccount, bool]:
    """Create or refresh the account for ``role``.

    Idempotent on the username: a second call resets the password, re-applies
    every precondition, and mints fresh keys, rather than creating a duplicate.
    Existing keys are revoked in the same transaction so an interrupted run
    cannot leave a live credential nobody holds.

    Args:
        role: Role name the suite refers to this account by.
        password: Plaintext password to set.
        with_api_keys: Whether to mint external-API keys as well.
        external_apis: Whether to leave outbound providers and AI enabled.
            False by default because every provider outside REData bills per
            call and this account is driven hard; turn it on only for a run
            that is specifically exercising the enrichment panels.

    Returns:
        Tuple of the provisioned account and whether the user row was created.
    """
    username = username_for(role)
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": email_for(role), "is_active": True},
    )

    user.email = email_for(role)
    user.is_active = True
    user.set_password(password)
    user.save(update_fields=["email", "is_active", "password"])

    _mark_email_verified(user)
    _clear_second_factors(user)

    profile = _prepare_profile(user, external_apis=external_apis)

    api_key = restricted_key = None
    if with_api_keys:
        # Revoked, not deleted: ApiKeyUsageLog rows hang off the key, and the
        # settings page shows a revoked key so its owner can see it went away.
        ApiKey.objects.for_user(user).active().update(revoked_at=timezone.now())
        api_key = _mint_key(user, name=f"integration-suite-{role}", scopes=_FULL_SCOPES)
        restricted_key = _mint_key(user, name=f"integration-suite-{role}-restricted", scopes=_RESTRICTED_SCOPES)

    account = ProvisionedAccount(
        role=role,
        username=username,
        email=user.email,
        password=password,
        api_key=api_key,
        scopes=list(_FULL_SCOPES) if with_api_keys else [],
        restricted_api_key=restricted_key,
        restricted_scopes=list(_RESTRICTED_SCOPES) if with_api_keys else [],
        profile_uuid=str(profile.uuid),
        profile_slug=profile.slug,
        is_staff=user.is_staff,
    )
    logger.info("integration: provisioned %s", account.redacted())
    return account, created


def provision(roles: Sequence[str] = DEFAULT_ROLES, *, password: str | None = None, with_api_keys: bool = True, external_apis: bool = False) -> ProvisionResult:
    """Provision every role in ``roles``.

    Args:
        roles: Role names to provision.
        password: Shared plaintext password. Generated when omitted.
        with_api_keys: Whether to mint external-API keys.
        external_apis: Whether to leave outbound providers and AI enabled.

    Returns:
        The accounts, plus which roles were newly created.
    """
    shared_password = password or generate_password()
    result = ProvisionResult()
    for role in roles:
        account, created = provision_account(role, password=shared_password, with_api_keys=with_api_keys, external_apis=external_apis)
        result.accounts.append(account)
        (result.created_roles if created else result.refreshed_roles).append(role)
    return result


def purge() -> list[str]:
    """Delete every provisioned integration account and everything it owns.

    Reuses ``hard_delete_profile`` rather than cascading from ``User.delete()``:
    that is the path that also removes the profile's stored files, and a purge
    that leaves uploaded media behind is not a purge.

    Returns:
        Usernames that were deleted.
    """
    from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

    deleted: list[str] = []
    for user in list(integration_users()):
        username = user.username
        profile = getattr(user, "profile", None)
        if profile is None:
            # An orphaned user with no profile would otherwise be selected on
            # every subsequent run and never go away.
            user.delete()
        else:
            hard_delete_profile(profile)
        deleted.append(username)
    if deleted:
        logger.info("integration: purged %d account(s): %s", len(deleted), ", ".join(deleted))
    return deleted


# -- internals -------------------------------------------------------------


def _mark_email_verified(user: User) -> None:
    """Ensure the account has a verified ``EmailVerification`` row.

    ``CustomLoginView.form_invalid`` looks for this row to tell an unverified
    account apart from a wrong password, and an unverified one is refused at
    login with an offer to resend an email nobody can receive.
    """
    EmailVerification.objects.update_or_create(user=user, defaults={"verified_at": timezone.now()})


def _clear_second_factors(user: User) -> None:
    """Remove anything that would interpose a challenge between password and session."""
    from urbanlens.dashboard.models.account.model import AccountKdf, BackupCode, TOTPDevice, WebAuthnCredential

    TOTPDevice.objects.filter(user=user).delete()
    WebAuthnCredential.objects.filter(user=user).delete()
    BackupCode.objects.filter(user=user).delete()
    # Without this the login form derives its credential in the browser from a
    # stored salt, and the plaintext in the manifest stops being what gets
    # posted - so a password reset here would not be a password reset there.
    AccountKdf.objects.filter(user=user).delete()


def _prepare_profile(user: User, *, external_apis: bool) -> Profile:
    """Put the profile in the state a signed-in test expects, and return it."""
    from urbanlens.dashboard.models.profile.model import Profile

    profile, _ = Profile.objects.get_or_create(user=user)

    profile.welcome_onboarding_complete = True
    profile.profile_setup_complete = True
    # These accounts are driven hard and repeatedly. Every provider outside
    # REData bills per call, so a suite that left these on would turn a test run
    # into a bill - the same reasoning as the demo seeder's.
    profile.external_apis_enabled = external_apis
    profile.ai_enabled = external_apis
    profile.save(
        update_fields=[
            "welcome_onboarding_complete",
            "profile_setup_complete",
            "external_apis_enabled",
            "ai_enabled",
        ],
    )
    profile.ensure_slug()

    _silence_email_delivery(profile)
    return profile


def _silence_email_delivery(profile: Profile) -> None:
    """Set every notification type to on-site delivery only.

    Iterates the model's fields rather than naming them, so a notification type
    added later is covered without anyone remembering to come back here. On-site
    rather than off entirely, because a test may well want to assert that a
    notification was raised - it just must never be posted to an address on a
    domain that cannot receive it.

    Args:
        profile: The profile whose preferences to rewrite.
    """
    from urbanlens.dashboard.models.notifications.meta.delivery_preference import DeliveryPreference
    from urbanlens.dashboard.models.notifications.model import NotificationPreference

    preferences, _ = NotificationPreference.objects.get_or_create(profile=profile)
    choice_values = set(DeliveryPreference.values)
    updated: list[str] = []
    # `_meta` is Django's documented model introspection API; the underscore is
    # historical namespacing, not privacy.
    for model_field in NotificationPreference._meta.get_fields():  # noqa: SLF001
        choices = getattr(model_field, "choices", None)
        if not choices or {value for value, _label in choices} != choice_values:
            continue
        if getattr(preferences, model_field.name) != DeliveryPreference.SITE:
            setattr(preferences, model_field.name, DeliveryPreference.SITE)
        updated.append(model_field.name)
    if updated:
        preferences.save(update_fields=updated)


def _mint_key(user: User, *, name: str, scopes: Sequence[str]) -> str:
    """Issue one API key with an explicit scope grant, returning its plaintext.

    ``generate_api_key`` writes the default four-scope grant, which is what a
    user gets from the settings page - there is no scope picker there yet. The
    grant is widened here by a direct update because ``ApiKey.scopes`` is
    ``editable=False``: not writable through a form, which is the point, but
    perfectly writable by code that has decided what the grant should be.
    """
    api_key, raw = generate_api_key(user, name)
    ApiKey.objects.filter(pk=api_key.pk).update(scopes=list(scopes))
    return raw
