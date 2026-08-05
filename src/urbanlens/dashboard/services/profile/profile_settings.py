"""Read and patch a profile's preferences as one flat document.

The site's own settings page (``controllers.settings.SettingsView``) splits
these across a dozen small ModelForms, each posted independently by its own
HTMX form. That shape suits a page with a dozen cards; it suits a sync client
badly, since fetching "the current settings" would mean knowing which form owns
which field. This module exposes the same writable surface as a single
allowlist, so the external API can serve one GET and one PATCH over it.

Two deliberate divergences from the internal view:

- A field belonging to a feature the user doesn't have is a **400 here**, where
  the internal view silently ignores the whole form. A browser user simply
  never sees a disabled card, but a sync client retrying a rejected field
  forever needs to be told the field is unavailable rather than watching its
  write vanish.
- Community-gated fields that ``Profile.save()`` coerces are **reported back
  post-coercion**, not rejected. That coercion is the model's own invariant
  (``community_enabled=False`` forces every visibility to "no one"), and a
  client that asked for something it can't have is better served by being
  shown what it actually got.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature
from urbanlens.dashboard.services.media.storage import allowed_user_dimension_values, allowed_user_video_height_values

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile.model import Profile

#: Mirrors ``forms.settings_form._DISCORD_USERNAME_RE`` - this is private
#: contact info rather than a public identity claim, so it stays permissive
#: (no discriminator-length enforcement) rather than duplicating Discord's own
#: 100-char ceiling.
_DISCORD_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._#-]{2,100}$")

#: Profile columns that hold a private contact method (ContactMethodsForm).
#: Free text with no uniqueness constraint - unlike the OAuth-linked login
#: identity this module's own docstring excludes, these are just a way for
#: someone else to reach you and carry no side effects on write.
_CONTACT_FIELDS: tuple[str, ...] = ("phone_number", "signal_username", "discord_username", "whatsapp_number", "telegram_username", "matrix_handle")

#: Read-through ``User`` fields exposed here rather than living on ``Profile``
#: itself - see ``apply_settings_patch``/``read_settings`` for why they need
#: their own handling instead of the flat ``getattr``/``setattr`` the rest of
#: ``SETTINGS_FIELDS`` uses.
_USER_PASSTHROUGH_FIELDS: tuple[str, ...] = ("first_name", "last_name")


class SettingsValidationError(Exception):
    """Raised when a settings patch is rejected, carrying per-field messages.

    Attributes:
        errors: Field name -> human-readable reason, shaped to be returned
            directly as a 400 body.
    """

    def __init__(self, errors: dict[str, str]) -> None:
        """Store the per-field errors.

        Args:
            errors: Field name -> human-readable reason.
        """
        self.errors = errors
        super().__init__("; ".join(f"{field}: {message}" for field, message in sorted(errors.items())))


#: The writable settings allowlist, mirroring the forms in
#: ``forms/settings_form.py`` that ``controllers.settings.SettingsView`` posts.
#:
#: Explicitly enumerated rather than derived from the model: a new preference
#: field on ``Profile`` must be added here on purpose before an external client
#: can read or write it, so the surface can never widen by accident.
#:
#: Account **login identity** (``username``, ``email``, and the linked OAuth
#: social-auth accounts) is deliberately absent - changing one of those is not
#: a preference sync, and the internal views apply uniqueness checks and side
#: effects (availability lookups, session/social-auth implications) that don't
#: belong behind a scope grant. ``first_name``/``last_name`` and the six
#: ``ContactMethodsForm`` fields below are a different thing - free-text
#: display/contact info with no uniqueness constraint and no side effects,
#: matching how ``controllers.userprofile.ProfileFieldUpdateView`` already
#: treats them (see its ``_USER_FIELDS``/``_PROFILE_CONTACT``, which this
#: mirrors) - so they belong here, unlike the identity fields above them.
SETTINGS_FIELDS: tuple[str, ...] = (
    # Name (User passthrough - see _USER_PASSTHROUGH_FIELDS below).
    "first_name",
    "last_name",
    # Contact methods (ContactMethodsForm).
    *_CONTACT_FIELDS,
    # Privacy visibilities (PrivacySettingsForm) - all community-gated.
    "profile_visibility",
    "comment_visibility",
    "friend_request_visibility",
    "photo_upload_visibility",
    "viewer_photo_filter",
    "trip_pin_location_visibility",
    "contact_visibility",
    "direct_message_visibility",
    "common_pins_visibility",
    # Direct messages (DirectMessageSettingsForm) - the three visibilities are
    # community-gated, the other two are not.
    "online_status_visibility",
    "read_receipt_visibility",
    "typing_indicator_visibility",
    "direct_message_delete_after",
    "allow_friend_recommendations",
    # Style (StyleSettingsForm).
    "theme_mode",
    "map_dark_mode",
    "guidance_level",
    "distance_units",
    # Map display (MapDisplayForm).
    "default_map_view",
    "cluster_radius",
    "use_pin_cache",
    "suggest_pin_restructure",
    # Map center (MapCenterForm). The cached/remembered coordinate fields are
    # excluded on purpose: they are server-maintained state, not preferences.
    "map_center_mode",
    "map_custom_latitude",
    "map_custom_longitude",
    "map_default_zoom",
    # Markup defaults (MarkupDefaultsForm).
    "markup_fill_color",
    "markup_fill_opacity",
    "markup_border_color",
    "markup_border_opacity",
    # Places layer (PlacesLayerForm) - gated behind SiteFeature.PLACES.
    "places_google_enabled",
    "places_nps_enabled",
    "places_wikipedia_enabled",
    # AI (AISettingsForm) - gated behind SiteFeature.AI.
    "ai_enabled",
    "ai_label_categories",
    "ai_label_tags",
    "ai_label_statuses",
    # Keyword tagging (KeywordTaggingSettingsForm).
    "keyword_tagging_enabled",
    "keyword_label_categories",
    "keyword_label_tags",
    "keyword_label_statuses",
    # History (HistorySettingsForm).
    "track_pin_visits",
    "track_routes",
    "track_geolocation",
    "track_device_scans",
    "generate_photo_keywords",
    # Community (CommunitySettingsForm).
    "community_enabled",
    "show_wiki_cover_photos",
    "auto_create_pin_article_from_wikipedia",
    "show_supporter_badge",
    # Pin suggestions (PinSuggestionSettingsForm).
    "pin_suggestions_enabled",
    "suggest_public_pins",
    "suggest_pins_from_photos",
    "suggest_pins_from_external_apis",
    # Wiki sync (WikiSyncSettingsForm) - all community-gated.
    "sync_rating_to_wiki",
    "sync_vulnerability_to_wiki",
    "sync_priority_to_wiki",
    "sync_danger_to_wiki",
    "sync_aliases",
    # External APIs (ExternalApiSettingsForm).
    "external_apis_enabled",
    # Storage downscaling (handled inline by SettingsView, not by a form).
    "image_downscale_max_dimension",
    "video_downscale_max_height",
)

#: Fields only writable while the owning site feature is enabled for the user.
_FEATURE_GATED_FIELDS: dict[str, SiteFeature] = {
    "ai_enabled": SiteFeature.AI,
    "ai_label_categories": SiteFeature.AI,
    "ai_label_tags": SiteFeature.AI,
    "ai_label_statuses": SiteFeature.AI,
    "places_google_enabled": SiteFeature.PLACES,
    "places_nps_enabled": SiteFeature.PLACES,
    "places_wikipedia_enabled": SiteFeature.PLACES,
}


def _validate_storage_dimensions(profile: Profile, data: dict[str, Any], errors: dict[str, str]) -> None:
    """Reject downscale caps above what the profile's plan entitles it to.

    Mirrors the inline checks in ``SettingsView``: ``None`` (meaning "no
    downscaling preference") is always allowed, any other value must appear in
    the entitled set.

    Args:
        profile: The profile being patched.
        data: The submitted patch.
        errors: Accumulator mutated in place with any failures.
    """
    if "image_downscale_max_dimension" in data:
        value = data["image_downscale_max_dimension"]
        if value is not None and value not in allowed_user_dimension_values(profile):
            errors["image_downscale_max_dimension"] = "That photo size is not available on your plan."
    if "video_downscale_max_height" in data:
        value = data["video_downscale_max_height"]
        if value is not None and value not in allowed_user_video_height_values(profile):
            errors["video_downscale_max_height"] = "That video quality is not available on your plan."


def _validate_discord_username(data: dict[str, Any], errors: dict[str, str]) -> None:
    """Reject a ``discord_username`` outside ``ContactMethodsForm``'s allowed character set.

    Args:
        data: The submitted patch.
        errors: Accumulator mutated in place with any failure.
    """
    value = data.get("discord_username")
    if value and not _DISCORD_USERNAME_RE.match(value):
        errors["discord_username"] = "2-100 characters: letters, digits, underscores, dots, hyphens, or #."


def apply_settings_patch(profile: Profile, data: dict[str, Any], *, user: User) -> list[str]:
    """Validate and apply a partial settings update to *profile* (and, for name fields, *user*) in memory.

    Does not save *profile* - the caller owns its transaction and the
    ``update_fields`` list, which is what the returned names are for.
    ``first_name``/``last_name`` are the one exception: they live on ``User``,
    not ``Profile``, so this function saves *user* itself immediately when
    either is touched, rather than asking the caller to manage a second
    model's save alongside the one it already owns.

    Args:
        profile: The profile to mutate.
        data: Submitted field -> value pairs. Only keys actually present are
            touched, so omitting a field differs from setting it null.
        user: The user whose feature entitlements gate the AI/Places fields,
            and whose ``first_name``/``last_name`` are written directly.
            Passed separately rather than read off ``profile.user`` so the
            caller's authenticated user is what's checked and written.

    Returns:
        The names of the changed *profile* fields, for the caller's own
        ``update_fields`` - never includes ``first_name``/``last_name``,
        which this function has already saved onto ``user`` itself.

    Raises:
        SettingsValidationError: If any submitted field is unknown, gated
            behind a feature the user lacks, outside the values their plan
            entitles them to, or (for ``discord_username``) outside the
            allowed character set.
    """
    errors: dict[str, str] = {}

    unknown = set(data) - set(SETTINGS_FIELDS)
    for field in unknown:
        errors[field] = "Unknown setting."

    for field, feature in _FEATURE_GATED_FIELDS.items():
        if field in data and not user_has_feature(user, feature):
            errors[field] = f"The {feature.label} feature is not enabled for your account."

    _validate_storage_dimensions(profile, data, errors)
    _validate_discord_username(data, errors)

    if errors:
        raise SettingsValidationError(errors)

    touched: list[str] = []
    user_touched: list[str] = []
    for field in SETTINGS_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field in _USER_PASSTHROUGH_FIELDS:
            if getattr(user, field) != value:
                setattr(user, field, value)
            user_touched.append(field)
            continue
        if getattr(profile, field) != value:
            setattr(profile, field, value)
        # Reported as touched even when the value matches: the caller builds
        # update_fields from this, and a no-op field costs one column in the
        # UPDATE, whereas omitting a field the model later coerces would drop
        # that coercion on the floor.
        touched.append(field)

    if user_touched:
        user.save(update_fields=user_touched)

    return touched


def read_settings(profile: Profile, *, user: User) -> dict[str, Any]:
    """Build the full settings document for *profile*.

    Includes read-only context a client needs to render a settings UI without a
    second round-trip: which features are available (so it can hide the AI and
    Places cards), which downscale values the plan permits, and the distance
    unit actually in effect once the profile's inferred fallback is applied.

    Args:
        profile: The profile to read. Should be re-read from the instance
            *after* any save, so community-gated coercions are reflected.
        user: The user whose feature entitlements are reported.

    Returns:
        Every allowlisted field's current value, plus the read-only keys
        ``updated``, ``effective_distance_units``, ``features``,
        ``allowed_image_dimensions`` and ``allowed_video_heights``.
    """
    payload: dict[str, Any] = {field: getattr(profile, field) for field in SETTINGS_FIELDS}
    payload["updated"] = profile.updated
    payload["effective_distance_units"] = profile.effective_distance_units
    payload["features"] = {
        "ai": user_has_feature(user, SiteFeature.AI),
        "places": user_has_feature(user, SiteFeature.PLACES),
    }
    payload["allowed_image_dimensions"] = sorted(allowed_user_dimension_values(profile))
    payload["allowed_video_heights"] = sorted(allowed_user_video_height_values(profile))
    return payload
