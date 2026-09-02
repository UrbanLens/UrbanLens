"""The assistant's place-evidence tool - "does this place have tunnels?" and friends.

Evidence-gathering, not a verdict from any single authoritative source. A
``RedataUndergroundGateway`` exists (``plugins/builtin/redata_underground.py``,
OSM-sourced mapped structures) and would answer this better than keyword
matching - it is deliberately not used here, the same "no REData" bypass
rationale as ``routing.py``/``weather.py``: the sandboxed AI worker must never
depend on REData being reachable at all. So this reads whatever the user's own
floorplan, this place's photos, and this place's wiki comments already say. A
tool result of "no_evidence" means nothing visible said so - never "no".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: How many keyword-matched images/comments to surface as evidence - enough to
#: substantiate a verdict without ballooning the tool result.
_MAX_EVIDENCE_ITEMS = 3


def _resolve_own_pin(context: ToolContext, pin_slug: str) -> Pin | None:
    """One of the requesting profile's own pins, with its location/place preloaded.

    Never resolves any other profile's pin - see ``Pin.objects.by_profile``.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    return Pin.objects.by_profile(context.profile).filter(slug=pin_slug.strip()).select_related("location", "location__place").first()


def _floorplan_evidence(pin: Pin, context: ToolContext) -> str | None:
    """A note about below-grade levels, from the plan the user would actually see for this place.

    Reuses ``resolve_floorplan_row`` rather than re-deriving its profile/community
    fallback: the personal-plan-first, then-published-if-the-wiki-is-visible rule
    lives there once, and ``Floorplan.objects.at()`` itself must never be called
    without ``profile=``/``community=`` (it returns every profile's plans otherwise).
    """
    from urbanlens.dashboard.models.floorplans.model import Floorplan

    place = pin.location.place
    if place is not None:
        from urbanlens.dashboard.services.floorplans.resolution import resolve_floorplan_row

        floorplan = resolve_floorplan_row(place, profile=context.profile)
    else:
        # A placeless plan is scoped to its pin, not a place - see
        # Floorplan.objects.for_place's docstring. Already pin-scoped via
        # _resolve_own_pin, so this can't reach another profile's plan.
        floorplan = Floorplan.objects.filter(place__isnull=True, pin=pin, profile=context.profile).first()
    if floorplan is None:
        return None
    below_grade = floorplan.floors.filter(level__lt=0).count()
    if below_grade == 0:
        return None
    return f"A floorplan on file shows {below_grade} level(s) below grade."


def _image_evidence(pin: Pin, context: ToolContext) -> list[dict[str, str]]:
    """Up to a few visible photo captions keyword-matched on this place."""
    from django.db.models import Q

    from urbanlens.dashboard.models.images.model import Image

    keyword = Q(caption__icontains="tunnel") | Q(labels__name__icontains="tunnel")
    # Narrow to this place's images first, then .visible_to() - it's eager and
    # resolves its allowed-uploader set from whatever the queryset already
    # matches, so narrowing after it would inspect every uploader on the site.
    images = Image.objects.filter(location=pin.location).filter(keyword).distinct().visible_to(context.profile)[:_MAX_EVIDENCE_ITEMS]
    return [{"caption": image.caption or "(tagged 'tunnel', no caption)"} for image in images]


def _comment_evidence(pin: Pin, context: ToolContext) -> list[dict[str, str]]:
    """Up to a few visible wiki comment snippets keyword-matched on this place."""
    from django.core.exceptions import ObjectDoesNotExist

    from urbanlens.dashboard.services.comments.comments import top_level_comment_queryset, visible_comment_tree

    place = pin.location.place
    if place is None:
        return []
    try:
        wiki = place.wiki
    except ObjectDoesNotExist:
        return []

    comments_qs = wiki.comments.filter(text__icontains="tunnel")
    top_level = list(top_level_comment_queryset(comments_qs)[: _MAX_EVIDENCE_ITEMS * 4])
    # Every visibility gate (comment-visibility settings, pending-scan, the @loc
    # mention gate) lives in visible_comment_tree - see its own module docstring
    # for why a second, ad hoc filter here would be how that quietly regresses.
    visible = visible_comment_tree(top_level, context.profile)

    snippets: list[dict[str, str]] = []
    for item in visible:
        candidates = [item.comment, *(reply.comment for reply in item.replies)]
        for comment in candidates:
            if "tunnel" in comment.text.lower():
                snippets.append({"snippet": comment.text[:160]})
        if len(snippets) >= _MAX_EVIDENCE_ITEMS:
            break
    return snippets[:_MAX_EVIDENCE_ITEMS]


class HasTunnelsArgs(BaseModel):
    pin_slug: str = Field(min_length=1, max_length=255)


def _has_tunnels(context: ToolContext, args: HasTunnelsArgs) -> dict[str, Any]:
    pin = _resolve_own_pin(context, args.pin_slug)
    if pin is None:
        return {"error": "pin_slug must be one of the user's own pins."}

    sources: list[str] = []
    floorplan_note = _floorplan_evidence(pin, context)
    if floorplan_note is not None:
        sources.append("floorplan")
    image_captions = _image_evidence(pin, context)
    if image_captions:
        sources.append("images")
    comment_snippets = _comment_evidence(pin, context)
    if comment_snippets:
        sources.append("comments")

    result: dict[str, Any] = {"verdict": "evidence" if sources else "no_evidence", "sources": sources}
    if floorplan_note is not None:
        result["floorplan_note"] = floorplan_note
    if image_captions:
        result["image_captions"] = image_captions
    if comment_snippets:
        result["comment_snippets"] = comment_snippets
    return result


register(
    ToolSpec(
        name="has_tunnels",
        description=(
            "Whether there's visible evidence of tunnels or below-grade levels at one of the user's own pins (pin_slug) - "
            "checked against any floorplan on file, photo captions/labels, and wiki comments. verdict is 'evidence' or "
            "'no_evidence' - 'no_evidence' means nothing visible says so, never state it as a confirmed 'no'."
        ),
        args_model=HasTunnelsArgs,
        handler=_has_tunnels,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.VISIBLE_SHARED,
        progress_label="Checking for tunnels…",
        action_label="Checked for tunnels",
        user_content_fields=frozenset({"caption", "snippet"}),
    ),
)
