"""Accounts, roles, features and the account lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.plugins.extensions import ActionHook, Choices, ExtensionPoint, FilterHook

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.db.models import Model

    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole
    from urbanlens.dashboard.services.profile.settings_groups import SettingsGroup


SITE_FEATURES = Choices(
    "subscriptions.site_features",
    "Features a role or the site default can grant, as ``value -> label``. Core's are the ``SiteFeature`` members; a plugin adds the features it gates, and checks them by value with ``user_has_feature``.",
)

NOTIFICATION_TYPES = Choices(
    "notifications.types",
    "Notification kinds beyond core's ``NotificationType`` members, as ``value -> label``. A plugin adds the kinds it sends; the value is what a ``Notification`` row stores.",
)

API_KEY_SCOPES = Choices(
    "api.key_scopes",
    "API-key scopes beyond core's ``ApiKeyScope`` members, as ``value -> label``. A plugin adds the scopes its endpoints require; a scope's consent text is its label.",
)

SUBSCRIPTION_ROLES: FilterHook[list[SubscriptionRole], [User]] = FilterHook(
    "subscriptions.roles",
    "Roles a user holds beyond their grants. Receives the list so far and the user.",
)

ACCESS_STATE_SENDERS: FilterHook[list[type[Model]], []] = FilterHook(
    "subscriptions.access_state_senders",
    "Models whose saves and deletes change a user's access, so the cached access state is dropped when they change.",
)

SITE_SETTINGS_SENDERS: FilterHook[list[type[Model]], []] = FilterHook(
    "site_settings.cache_senders",
    "Models whose saves and deletes change what the request-scoped site settings cache holds.",
)


ACCOUNT_DELETING: ActionHook[[User]] = ActionHook(
    "accounts.deleting",
    "Fired just before an account and its rows are permanently deleted, so a plugin can release what it holds elsewhere (cancel a subscription, revoke a token). Raising stops the deletion; the next sweep retries it.",
    on_error="raise",
)

HOTKEY_ACTIONS: ExtensionPoint[str] = ExtensionPoint(
    "accounts.hotkey_actions",
    "Ids of the keyboard-shortcut actions a plugin registers client-side with ``registerHotkeyDefault``, so a user's override of one is kept when settings are saved.",
)

SETTINGS_EXPORT: FilterHook[dict[str, Any], [Profile]] = FilterHook(
    "accounts.settings_export",
    "The settings written to a user's data export. A plugin that stores per-user preferences adds them.",
)

SETTINGS_GROUPS: ExtensionPoint[SettingsGroup] = ExtensionPoint(
    "accounts.settings_groups",
    "Preferences a plugin keeps outside the profile row. Each group names its fields, and is read, validated and written with the account settings the API serves, and restored from a data export.",
)

SETTINGS_IMPORT: ActionHook[[Profile, dict[str, Any]]] = ActionHook(
    "accounts.settings_import",
    "Fired while a data export's settings are restored. Receives the profile and the whole settings document, so a plugin can restore what it added through ``accounts.settings_export``.",
)

EXTERNAL_APIS_DISABLED: ActionHook[[Profile]] = ActionHook(
    "accounts.external_apis_disabled",
    "Fired when onboarding turns external APIs off for an account. A plugin that keeps an external-data preference of its own turns it off too.",
)

__all__ = [
    "ACCESS_STATE_SENDERS",
    "ACCOUNT_DELETING",
    "API_KEY_SCOPES",
    "EXTERNAL_APIS_DISABLED",
    "HOTKEY_ACTIONS",
    "NOTIFICATION_TYPES",
    "SETTINGS_EXPORT",
    "SETTINGS_GROUPS",
    "SETTINGS_IMPORT",
    "SITE_FEATURES",
    "SITE_SETTINGS_SENDERS",
    "SUBSCRIPTION_ROLES",
]
