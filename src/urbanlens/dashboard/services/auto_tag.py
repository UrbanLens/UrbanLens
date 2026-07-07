"""Auto-tagging service: keyword and AI-based badge suggestion for pins and wikis.

Pipeline per badge kind:
  1. Keyword match - badge name patterns (CATEGORY_PATTERNS) + badge.keywords field.
  2. AI match     - remaining eligible badges sent to LLM as a constrained list.

Callers use AutoTagService.suggest_for_pin / suggest_for_wiki; both return
the matched Badge instances and optionally apply them immediately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

# Maps badge kind → Profile field that enables auto-tagging for that kind.
_KIND_PREF: dict[str, str] = {
    "category": "ai_badge_categories",
    "tag": "ai_badge_tags",
    "status": "ai_badge_statuses",
}

# Fallback category list shown to AI when no global badges exist yet.
_FALLBACK_EXAMPLES = (
    "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, "
    "Firehouse, Fire Tower, Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, "
    "Library, Lighthouse, Mall, Mansion, Military Base, Monument, Police Station, Power Plant, "
    "Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel"
)


class AutoTagService:
    """Suggests and optionally applies badges to a Pin or Location.

    Uses a two-stage pipeline:

    1. **Keyword matching** - checks CATEGORY_PATTERNS (built-in regex patterns keyed
       by badge name) and each badge's custom ``keywords`` field.  No API call.
    2. **AI matching** - sends a constrained list of remaining eligible badge names to
       the configured LLM gateway and validates the returned names against the list.

    Args:
        kinds: Badge kinds to process.  Defaults to ``["category"]``.
        max_badges: Maximum suggestions returned *per kind*.  ``None`` means no
            limit (accept all keyword hits + everything the AI proposes).
            Defaults to ``None`` (multiple).
    """

    _DEFAULT_KINDS: tuple[str, ...] = ("category",)

    def __init__(
        self,
        kinds: list[str] | tuple[str, ...] | None = None,
        max_badges: int | None = None,
    ) -> None:
        self.kinds = list(kinds) if kinds is not None else list(self._DEFAULT_KINDS)
        self.max_badges = max_badges

    # -- public entry points --------------------------------------------------

    def suggest_for_pin(self, pin: Pin, *, apply: bool = False) -> list[Badge]:
        """Suggest (and optionally apply) badges for a Pin.

        Respects the pin owner's per-kind AI preference flags.  Only badges
        visible to that user (global + user-owned) are considered.

        Args:
            pin: Target Pin instance.
            apply: When True, attach all matched badges before returning.

        Returns:
            Matched Badge instances across all configured kinds.
        """
        profile = getattr(pin, "profile", None)
        results: list[Badge] = []
        for kind in self.kinds:
            if profile is not None and not self._kind_enabled_for_profile(kind, profile):
                logger.debug("Auto-tagging kind '%s' disabled for profile %s", kind, profile.pk)
                continue
            eligible = self._eligible_badges(kind, profile=profile)
            matched = self._match(pin, eligible, kind)
            if apply and matched:
                pin.badges.add(*matched)
            results.extend(matched)
        return results

    def suggest_for_wiki(self, wiki: Wiki, *, apply: bool = False) -> list[Badge]:
        """Suggest (and optionally apply) badges for a community Wiki.

        A Wiki is shared/global, so only global badges (``profile=None``) are
        considered and no user preference check is performed.

        Args:
            wiki: Target Wiki instance.
            apply: When True, attach all matched badges before returning.

        Returns:
            Matched Badge instances across all configured kinds.
        """
        results: list[Badge] = []
        for kind in self.kinds:
            eligible = self._eligible_badges(kind, profile=None)
            matched = self._match(wiki, eligible, kind)
            if apply and matched:
                wiki.badges.add(*matched)
            results.extend(matched)
        return results

    # -- eligibility ----------------------------------------------------------

    @staticmethod
    def _kind_enabled_for_profile(kind: str, profile: Profile) -> bool:
        """Return False when the user has disabled auto-tagging for this badge kind.

        Args:
            kind: Badge kind string (e.g. ``"category"``).
            profile: Owner profile to check.

        Returns:
            True if auto-tagging is permitted for this kind and profile.
        """
        if not getattr(profile, "ai_enabled", True):
            return False
        pref_field = _KIND_PREF.get(kind)
        return bool(getattr(profile, pref_field, True)) if pref_field else True

    @staticmethod
    def _eligible_badges(kind: str, *, profile: Profile | None) -> list[Badge]:
        """Return badges eligible for auto-tagging.

        Excludes protected badges and those with ``allow_auto_tag=False``.
        For Pin targets (profile given) both global and user-owned badges are
        included; for Location targets only global badges.

        Args:
            kind: Badge kind to filter by.
            profile: Owning profile for Pin targets; ``None`` for Location targets.

        Returns:
            Ordered list of eligible Badge instances.
        """
        from django.db.models import Q

        from urbanlens.dashboard.models.badges.model import Badge

        qs = Badge.objects.filter(kind=kind, allow_auto_tag=True).exclude(is_protected=True)
        if profile is not None:
            qs = qs.filter(Q(profile__isnull=True) | Q(profile=profile))
        else:
            qs = qs.filter(profile__isnull=True)
        return list(qs.order_by("name"))

    # -- matching pipeline ----------------------------------------------------

    def _match(
        self,
        target: Pin | Wiki,
        eligible: list[Badge],
        kind: str,
    ) -> list[Badge]:
        """Run the full keyword → AI pipeline and return up to max_badges results.

        Args:
            target: Pin or Location being tagged.
            eligible: Pre-filtered list of candidate badges.
            kind: Badge kind (used for AI instructions).

        Returns:
            Matched Badge instances.
        """
        if not eligible:
            return []

        text = self._build_keyword_text(target)
        matched = self._keyword_match(eligible, text)
        remaining = [b for b in eligible if b not in matched]

        need_more = self.max_badges is None or len(matched) < self.max_badges
        if need_more and remaining:
            ai_results = self._ai_match(target, remaining, kind)
            # Avoid duplicates (keyword match already got some).
            seen = {b.pk for b in matched}
            for b in ai_results:
                if b.pk not in seen:
                    matched.append(b)
                    seen.add(b.pk)

        if self.max_badges is not None:
            matched = matched[: self.max_badges]
        return matched

    # -- keyword matching ------------------------------------------------------

    @staticmethod
    def _build_keyword_text(target: Pin | Wiki) -> str:
        """Build the text corpus for keyword matching (name + place name only).

        Addresses are intentionally excluded to avoid false positives such as
        "Church Street" matching the Church category.

        Args:
            target: Pin or Wiki instance.

        Returns:
            Space-joined text of relevant name fields.
        """
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        parts: list[str] = []
        official = getattr(target, "meaningful_official_name", None)
        name: str | None = official if official is not None else None
        if name is None:
            raw = getattr(target, "official_name", None)
            if raw and is_meaningful_name(raw):
                name = raw
        if name:
            parts.append(name)
        # A community Wiki also carries an editable name distinct from official_name.
        # (Pin.name is a personal label and is intentionally excluded here.)
        if type(target).__name__ == "Wiki":
            community_name = getattr(target, "name", None)
            if community_name and is_meaningful_name(community_name) and community_name not in parts:
                parts.append(community_name)
        return " ".join(parts)

    @staticmethod
    def _badge_matches_text(
        badge: Badge,
        text: str,
        compiled_patterns: dict,
    ) -> bool:
        """Return True if this badge's name patterns or custom keywords match text.

        Matching order:
        1. Built-in CATEGORY_PATTERNS keyed by badge name (case-insensitive).
        2. User-defined ``badge.keywords`` (comma-separated phrase list).

        Args:
            badge: Badge to test.
            text: Text to search in.
            compiled_patterns: Pre-compiled pattern dict from
                ``keywords._get_compiled()``.

        Returns:
            True on first match found.
        """
        # 1. Built-in regex patterns.
        badge_name_lower = badge.name.lower()
        for cat_key, patterns in compiled_patterns.items():
            if cat_key.lower() == badge_name_lower:
                for pat in patterns:
                    if pat.search(text):
                        return True

        # 2. User-defined keywords (case-insensitive substring search).
        if badge.keywords:
            lower_text = text.lower()
            for raw_kw in badge.keywords.split(","):
                needle = raw_kw.strip().lower()
                if needle and needle in lower_text:
                    return True

        return False

    def _keyword_match(self, eligible: list[Badge], text: str) -> list[Badge]:
        """Return all eligible badges that match the text via keywords.

        Args:
            eligible: Candidate badges.
            text: Text to match against (from ``_build_keyword_text``).

        Returns:
            Matched badges in eligibility order.
        """
        if not text:
            return []

        from urbanlens.dashboard.services.ai.keywords import _get_compiled

        compiled = _get_compiled()
        matched: list[Badge] = []
        for badge in eligible:
            if self._badge_matches_text(badge, text, compiled):
                matched.append(badge)
                if self.max_badges and len(matched) >= self.max_badges:
                    break
        return matched

    # -- AI matching -----------------------------------------------------------

    def _ai_match(
        self,
        target: Pin | Wiki,
        eligible: list[Badge],
        kind: str,
    ) -> list[Badge]:
        """Use the LLM gateway to select badges from the eligible list.

        Args:
            target: Pin or Location being tagged.
            eligible: Badges not yet matched by keywords.
            kind: Badge kind (used for instructions wording).

        Returns:
            Matched Badge instances validated against the eligible list.
        """
        from urbanlens.dashboard.services.ai.factory import get_gateway

        prompt = self._build_prompt(target)
        if not prompt:
            return []

        instructions = self._build_instructions(eligible, kind)
        gateway = get_gateway("category_suggestions", instructions=instructions)
        if not gateway:
            return []

        names = gateway.send_prompt_list(prompt, max_results=self.max_badges)
        name_to_badge = {b.name.lower(): b for b in eligible}
        results: list[Badge] = []
        for raw_name in names:
            badge = name_to_badge.get(raw_name.lower())
            if badge:
                results.append(badge)
            else:
                logger.debug("AI returned '%s' not in eligible list; discarding", raw_name)
        return results

    @staticmethod
    def _build_instructions(eligible: list[Badge], kind: str) -> str:
        """Build the AI system instructions with the constrained badge list.

        Args:
            eligible: Badges the AI may choose from.
            kind: Badge kind name used in the instruction text.

        Returns:
            Instruction string to pass to the gateway.
        """
        if eligible:
            names = ", ".join(b.name for b in eligible)
            return (
                f"Identify which {kind}(s) best describe the following location.\n\n"
                f"You MUST choose ONLY from this list: {names}.\n\n"
                "Multiple selections are fine when the location clearly fits several entries. "
                "Do not invent names that are not in the list. "
                "Wrap each selection in ANSWER tags: <ANSWER>Factory</ANSWER><ANSWER>Ruins</ANSWER>. "
                "If nothing in the list fits well, return no ANSWER tags."
            )
        return f"Identify the {kind}(s) that best describe this urbex location. Examples: {_FALLBACK_EXAMPLES}. Wrap each answer: <ANSWER>Factory</ANSWER>. Return only well-fitting entries."

    @staticmethod
    def _build_prompt(target: Pin | Wiki) -> str:
        """Build the location-context prompt to send to the AI.

        Includes address, place name, LocationCache data (for Pin targets), and
        user-supplied fields wrapped in injection-safe USER_DATA delimiters.

        Args:
            target: Pin or Location instance.

        Returns:
            Prompt string, or empty string if no usable data is available.
        """
        from urbanlens.dashboard.services.ai.scanner import wrap_user_data
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        prompt = ""

        if address := getattr(target, "address", None):
            prompt += f"address: {address}\n"

        if getattr(target, "has_place_name", lambda: False)():
            if place := getattr(target, "place_name", None):
                prompt += f"google maps place name: {place}\n"

        # LocationCache enrichment (only available when target is a Pin with a linked Location).
        location = getattr(target, "location", None)
        if location is not None:
            try:
                from urbanlens.dashboard.models.cache.location_cache import LocationCache

                for source in ("google_places", "nominatim"):
                    cached = LocationCache.get_fresh(location, source)
                    if cached and cached.data:
                        data = cached.data
                        place_types = data.get("types") or data.get("category") or []
                        if isinstance(place_types, list) and place_types:
                            prompt += f"place types ({source}): {', '.join(str(t) for t in place_types[:8])}\n"
                        place_desc = data.get("description") or data.get("display_name") or ""
                        if place_desc:
                            prompt += f"place description ({source}): {place_desc[:300]}\n"
            except Exception:
                logger.debug("Could not load LocationCache data for %r", target, exc_info=True)

        # User-supplied fields wrapped to guard against prompt injection.
        user_fields = ""
        official = getattr(target, "meaningful_official_name", None)
        name: str | None = official if official is not None else None
        if name is None:
            raw = getattr(target, "official_name", None)
            if raw and is_meaningful_name(raw):
                name = raw
        if name:
            user_fields += f"location title: {name}\n"
        if description := getattr(target, "description", None):
            user_fields += f"description: {description}\n"
        if user_fields:
            prompt += wrap_user_data(user_fields) + "\n"

        return prompt
