"""Auto-tagging service: label suggestion for pins and wikis.
A **wiki** has no owner and so no per-user REData taxonomy to match against; it keeps the keyword stage (label name patterns plus each label's ``keywords`` field), which needs no external service and no user identity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Minimum REData confidence before a label is applied automatically.
#: A starting value, not a measured one: applying a label is cheap to undo but annoying to find, so
#: this errs high and should be tuned against real suggestion data rather than guessed at again.
REDATA_CONFIDENCE_FLOOR = 0.6

# Maps label kind → Profile field that enables AI-based auto-tagging for that kind.
_AI_KIND_PREF: dict[str, str] = {
    "category": "ai_label_categories",
    "tag": "ai_label_tags",
    "status": "ai_label_statuses",
}

# Maps label kind → Profile field that enables keyword-based auto-tagging for that kind.
_KEYWORD_KIND_PREF: dict[str, str] = {
    "category": "keyword_label_categories",
    "tag": "keyword_label_tags",
    "status": "keyword_label_statuses",
}

# Fallback category list shown to AI when no global labels exist yet.
_FALLBACK_EXAMPLES = (
    "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, "
    "Firehouse, Fire Tower, Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, "
    "Library, Lighthouse, Mall, Mansion, Military Base, Monument, Police Station, Power Plant, "
    "Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel"
)


class AutoTagService:
    """Suggests and optionally applies labels to a Pin or Location.

    Args:
        kinds: Label kinds to process.
        max_labels: Maximum suggestions returned *per kind*."""

    _DEFAULT_KINDS: tuple[str, ...] = ("category",)

    def __init__(
        self,
        kinds: list[str] | tuple[str, ...] | None = None,
        max_labels: int | None = None,
    ) -> None:
        self.kinds = list(kinds) if kinds is not None else list(self._DEFAULT_KINDS)
        self.max_labels = max_labels

    # -- public entry points --------------------------------------------------

    def suggest_for_pin(self, pin: Pin, *, apply: bool = False) -> list[Label]:
        """Suggest (and optionally apply) labels for a Pin.
        Only labels visible to that user (global + user-owned) are considered.

        Returns:
            Matched Label instances across all configured kinds."""
        profile = getattr(pin, "profile", None)
        results: list[Label] = []
        for kind in self.kinds:
            if profile is not None:
                do_redata = self._redata_kind_enabled_for_profile(kind, profile)
                do_ai = self._ai_kind_enabled_for_profile(kind, profile)
                if not do_redata and not do_ai:
                    logger.debug("Auto-tagging kind '%s' disabled for profile %s", kind, profile.pk)
                    continue
            else:
                do_redata = do_ai = True
            eligible = self._eligible_labels(kind, profile=profile)
            matched = self._redata_match(pin, eligible, kind) if do_redata else []
            if do_ai:
                remaining = [label for label in eligible if label not in matched]
                matched = matched + self._match(pin, remaining, kind, do_keyword=False, do_ai=True)
            if apply and matched:
                to_apply = self._exclude_removed(pin, matched)
                if to_apply:
                    pin.labels.add(*to_apply)
            results.extend(matched)
        return results

    def suggest_for_wiki(self, wiki: Wiki, *, apply: bool = False) -> list[Label]:
        """Suggest (and optionally apply) labels for a community Wiki.
        A Wiki is shared/global, so only global labels (``profile=None``) are considered and no user preference check is performed.

        Returns:
            Matched Label instances across all configured kinds."""
        results: list[Label] = []
        for kind in self.kinds:
            eligible = self._eligible_labels(kind, profile=None)
            matched = self._match(wiki, eligible, kind)
            if apply and matched:
                to_apply = self._exclude_removed(wiki, matched)
                if to_apply:
                    wiki.labels.add(*to_apply)
            results.extend(matched)
        return results

    # -- deletion-awareness -----------------------------------------------------

    @staticmethod
    def _exclude_removed(target: Pin | Wiki, matched: list[Label]) -> list[Label]:
        """Drop any matched label the user has already removed from this target."""
        from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval

        if type(target).__name__ == "Wiki":
            removed_ids = set(WikiAutoRemoval.objects.filter(wiki=target, kind=AutoRemovalKind.LABEL).values_list("value", flat=True))
        else:
            removed_ids = set(PinAutoRemoval.objects.filter(pin=target, kind=AutoRemovalKind.LABEL).values_list("value", flat=True))
        return [label for label in matched if str(label.pk) not in removed_ids]

    # -- eligibility ----------------------------------------------------------

    @staticmethod
    def _ai_kind_enabled_for_profile(kind: str, profile: Profile) -> bool:
        """Return False when the user has disabled AI-based auto-tagging for this label kind.

        Returns:
            True if AI-based auto-tagging is permitted for this kind and profile."""
        if not getattr(profile, "ai_enabled", True) or not getattr(profile, "external_apis_enabled", True):
            return False
        pref_field = _AI_KIND_PREF.get(kind)
        return bool(getattr(profile, pref_field, True)) if pref_field else True

    @staticmethod
    def _keyword_kind_enabled_for_profile(kind: str, profile: Profile) -> bool:
        """Return False when the user has disabled keyword-based auto-tagging for this label kind.
        Keyword matching makes no external API call, so it isn't gated on ``external_apis_enabled`` - only the user's own keyword-tagging toggles.

        Returns:
            True if keyword-based auto-tagging is permitted for this kind and profile."""
        if not getattr(profile, "keyword_tagging_enabled", True):
            return False
        pref_field = _KEYWORD_KIND_PREF.get(kind)
        return bool(getattr(profile, pref_field, True)) if pref_field else True

    @staticmethod
    def _redata_kind_enabled_for_profile(kind: str, profile: Profile) -> bool:
        """Whether REData-sourced auto-tagging applies for this kind and profile.

        Returns:
            True when this profile may have labels of this kind applied automatically from REData's suggestions."""
        from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
        from urbanlens.dashboard.models.subscriptions.model import SiteFeature, user_has_feature

        if kind not in {KIND_CATEGORY, KIND_TAG} or getattr(profile, "disable_auto_tagging", False):
            return False
        user = getattr(profile, "user", None)
        return bool(user is not None and user_has_feature(user, SiteFeature.AUTO_TAGGING))

    @staticmethod
    def _redata_match(pin: Pin, eligible: list[Label], kind: str) -> list[Label]:
        """Labels REData suggests for this pin, restricted to the eligible set.
        REData answers about the profile's whole taxonomy, so the result is filtered back through ``eligible`` - which is what enforces the per-label opt-out and the protected-label exclusion here, rather than trusting the upstream answer to respect either.

        Returns:
            Matched labels, highest confidence first; empty when REData is unconfigured, fails, or suggests nothing above the floor."""
        from urbanlens.dashboard.services.labels.redata_suggestions import get_suggestions

        suggestions = get_suggestions(pin)
        if not suggestions:
            return []
        allowed = {label.pk for label in eligible}
        matched = [label for label, confidence in suggestions if label.pk in allowed and confidence >= REDATA_CONFIDENCE_FLOOR]
        if matched:
            logger.debug("REData auto-tag matched %d %s label(s) for pin %s", len(matched), kind, pin.pk)
        return matched

    @staticmethod
    def _eligible_labels(kind: str, *, profile: Profile | None) -> list[Label]:
        """Return labels eligible for auto-tagging.
        For Pin targets (profile given) both global and user-owned labels are included; for Location targets only global labels.

        Returns:
            Ordered list of eligible Label instances."""
        from django.db.models import Q

        from urbanlens.dashboard.models.labels.model import Label

        qs = Label.objects.filter(kind=kind, allow_auto_tag=True).exclude(is_protected=True)
        if profile is not None:
            qs = qs.filter(Q(profile__isnull=True) | Q(profile=profile))
        else:
            qs = qs.filter(profile__isnull=True)
        return list(qs.order_by("name"))

    # -- matching pipeline ----------------------------------------------------

    def _match(
        self,
        target: Pin | Wiki,
        eligible: list[Label],
        kind: str,
        *,
        do_keyword: bool = True,
        do_ai: bool = True,
    ) -> list[Label]:
        """Run the keyword and/or AI matching stages and return up to max_labels results.

        Returns:
            Matched Label instances."""
        if not eligible:
            return []

        matched: list[Label] = []
        remaining = eligible
        if do_keyword:
            text = self._build_keyword_text(target)
            matched = self._keyword_match(eligible, text)
            remaining = [b for b in eligible if b not in matched]

        need_more = self.max_labels is None or len(matched) < self.max_labels
        if do_ai and need_more and remaining:
            ai_results = self._ai_match(target, remaining, kind)
            # Avoid duplicates (keyword match already got some).
            seen = {b.pk for b in matched}
            for b in ai_results:
                if b.pk not in seen:
                    matched.append(b)
                    seen.add(b.pk)

        if self.max_labels is not None:
            matched = matched[: self.max_labels]
        return matched

    # -- keyword matching ------------------------------------------------------

    @staticmethod
    def _build_keyword_text(target: Pin | Wiki) -> str:
        """Build the text corpus for keyword matching (name + place name only).
        Addresses are intentionally excluded to avoid false positives such as "Church Street" matching the Church category.

        Returns:
            Space-joined text of relevant name fields."""
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
    def _label_matches_text(
        label: Label,
        text: str,
        compiled_patterns: dict,
    ) -> bool:
        """Return True if this label's name patterns or custom keywords match text.

        Returns:
            True on first match found."""
        # 1. Built-in regex patterns.
        label_name_lower = label.name.lower()
        for cat_key, patterns in compiled_patterns.items():
            if cat_key.lower() == label_name_lower:
                for pat in patterns:
                    if pat.search(text):
                        return True

        # 2. User-defined keywords (case-insensitive substring search).
        if label.keywords:
            lower_text = text.lower()
            for raw_kw in label.keywords.split(","):
                needle = raw_kw.strip().lower()
                if needle and needle in lower_text:
                    return True

        return False

    def _keyword_match(self, eligible: list[Label], text: str) -> list[Label]:
        """Return all eligible labels that match the text via keywords.

        Returns:
            Matched labels in eligibility order."""
        if not text:
            return []

        from urbanlens.dashboard.services.ai.keywords import _get_compiled

        compiled = _get_compiled()
        matched: list[Label] = []
        for label in eligible:
            if self._label_matches_text(label, text, compiled):
                matched.append(label)
                if self.max_labels and len(matched) >= self.max_labels:
                    break
        return matched

    # -- AI matching -----------------------------------------------------------

    def _ai_match(
        self,
        target: Pin | Wiki,
        eligible: list[Label],
        kind: str,
    ) -> list[Label]:
        """Use the LLM gateway to select labels from the eligible list.

        Returns:
            Matched Label instances validated against the eligible list."""
        from urbanlens.dashboard.services.ai.factory import get_gateway

        prompt = self._build_prompt(target)
        if not prompt:
            return []

        instructions = self._build_instructions(eligible, kind)
        gateway = get_gateway("category_suggestions", instructions=instructions)
        if not gateway:
            return []

        names = gateway.send_prompt_list(prompt, max_results=self.max_labels)
        name_to_label = {b.name.lower(): b for b in eligible}
        results: list[Label] = []
        for raw_name in names:
            label = name_to_label.get(raw_name.lower())
            if label:
                results.append(label)
            else:
                logger.debug("AI returned '%s' not in eligible list; discarding", raw_name)
        return results

    @staticmethod
    def _build_instructions(eligible: list[Label], kind: str) -> str:
        """Build the AI system instructions with the constrained label list.

        Returns:
            Instruction string to pass to the gateway."""
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

        Returns:
            Prompt string, or empty string if no usable data is available."""
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
