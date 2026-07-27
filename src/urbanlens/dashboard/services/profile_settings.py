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

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature
from urbanlens.dashboard.services.storage import allowed_user_dimension_values, allowed_user_video_height_values

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile.model import Profile


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
#: Account *identity* (email, Discord handle) is deliberately absent - changing
#: a login identifier is not a preference sync, and the internal form applies
#: uniqueness checks and side effects that don't belong behind a scope grant.
SETTINGS_FIELDS: tuple[str, ...] = (
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
    "generate_photo_keywords",
    # Community (CommunitySettingsForm).
    "community_enabled",
    "show_wiki_cover_photos",
    "auto_create_pin_article_from_wikipedia",
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


def apply_settings_patch(profile: Profile, data: dict[str, Any], *, user: User) -> list[str]:
    """Validate and apply a partial settings update to *profile* in memory.

    Does not save - the caller owns the transaction and the ``update_fields``
    list, which is what the returned names are for.

    Args:
        profile: The profile to mutate.
        data: Submitted field -> value pairs. Only keys actually present are
            touched, so omitting a field differs from setting it null.
        user: The user whose feature entitlements gate the AI/Places fields.
            Passed separately rather than read off ``profile.user`` so the
            caller's authenticated user is what's checked.

    Returns:
        The names of the fields that were changed, for ``update_fields``.

    Raises:
        SettingsValidationError: If any submitted field is unknown, gated
            behind a feature the user lacks, or outside the values their plan
            entitles them to.
    """
    errors: dict[str, str] = {}

    unknown = set(data) - set(SETTINGS_FIELDS)
    for field in unknown:
        errors[field] = "Unknown setting."

    for field, feature in _FEATURE_GATED_FIELDS.items():
        if field in data and not user_has_feature(user, feature):
            errors[field] = f"The {feature.label} feature is not enabled for your account."

    _validate_storage_dimensions(profile, data, errors)

    if errors:
        raise SettingsValidationError(errors)

    touched: list[str] = []
    for field in SETTINGS_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if getattr(profile, field) != value:
            setattr(profile, field, value)
        # Reported as touched even when the value matches: the caller builds
        # update_fields from this, and a no-op field costs one column in the
        # UPDATE, whereas omitting a field the model later coerces would drop
        # that coercion on the floor.
        touched.append(field)

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
