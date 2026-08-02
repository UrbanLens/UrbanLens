"""Full wiki-detail payload for the external API's ``GET /wikis/{location_slug}/``.

Deliberately shaped like ``services.pins.pin_detail.build_pin_detail`` - same
module layout, same ``_isoformat_or_none``/``_boundary_geojson`` helpers, same
"assemble a plain JSON-safe dict, let the serializer document it" contract - so
a reader who has understood one immediately understands the other.

The privacy-sensitive parts are delegated rather than reimplemented:
``services.wiki.community_counts.wiki_community_summary`` owns the pinned-user count
and the ``first_pinned`` fuzzing, and ``WikiStatVote.objects.composite`` owns
the per-field vote counts. Nothing here recomputes either.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identities
from urbanlens.dashboard.services.wiki.articles import latest_revision_id
from urbanlens.dashboard.services.wiki.community_counts import wiki_community_summary
from urbanlens.dashboard.services.wiki.wiki_aliases import alias_is_current_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


def _isoformat_or_none(value: Any) -> str | None:
    """Return ``value.isoformat()``, or None when *value* is None."""
    return value.isoformat() if value is not None else None


def _boundary_geojson(wiki: Wiki) -> dict[str, Any] | None:
    """The wiki's effective property boundary as a GeoJSON geometry dict, if any.

    Args:
        wiki: The wiki whose boundary to resolve.

    Returns:
        A GeoJSON geometry dict, or None when the place has no boundary.
    """
    polygon = Boundary.objects.effective_polygon_for_wiki(wiki, BoundaryType.PROPERTY)
    return json.loads(polygon.geojson) if polygon is not None else None


def masked_editor_name(profile: Profile | None, viewer: Profile) -> str | None:
    """Resolve an editor's display name as *viewer* is permitted to see it.

    Wiki content is authored by many people a viewer may have no standing to
    see, so every attributed name on this surface goes through the same
    identity-visibility masking the comment thread uses.

    Args:
        profile: The attributed profile, or None for a system/deleted author.
        viewer: The requesting profile.

    Returns:
        The masked display name, or None when there is no attributed author.
    """
    if profile is None:
        return None
    identity = resolve_visible_identities(viewer, [profile]).get(profile.pk) or {}
    display_name = identity.get("display_name")
    return str(display_name) if display_name else str(profile)


def _article_summary(wiki: Wiki, profile: Profile) -> dict[str, Any] | None:
    """Summarize the wiki's article without shipping its full body.

    Args:
        wiki: The wiki whose article to summarize.
        profile: The requesting profile, for author masking.

    Returns:
        A summary dict, or None when the wiki has no article yet.
    """
    from urbanlens.dashboard.services.wiki.articles import get_article

    article = get_article(wiki=wiki)
    if article is None:
        return None
    return {
        "id": article.pk,
        "word_count": article.word_count(),
        "updated": _isoformat_or_none(article.updated),
        "last_edited_by": masked_editor_name(article.last_edited_by, profile),
        "base_revision_id": latest_revision_id(article),
    }


def _stats(wiki: Wiki, profile: Profile) -> dict[str, dict[str, Any]]:
    """The four community stat composites plus the viewer's own votes.

    Args:
        wiki: The wiki whose stats to read.
        profile: The requesting profile, for ``my_vote``.

    Returns:
        ``{field: {rounded, exact, count, my_vote}}`` for every stat field.
        ``count`` is already privacy-fuzzed by the queryset's composite method.
    """
    stats: dict[str, dict[str, Any]] = {}
    for field in WikiStatField.values:
        composite = WikiStatVote.objects.composite(wiki, field)
        stats[field] = {
            "rounded": composite.rounded,
            "exact": composite.exact,
            "count": composite.count,
            "my_vote": WikiStatVote.objects.my_vote(wiki, field, profile),
        }
    return stats


def build_wiki_detail(wiki: Wiki, location: Location, profile: Profile) -> dict[str, Any]:
    """Assemble the full detail payload for one wiki.

    Args:
        wiki: The wiki to serialize. Caller is responsible for the visibility
            check - this never gates access itself (see
            ``services.wiki.wiki_access.resolve_visible_wiki``, which every caller
            must go through first).
        location: The wiki's Location, already resolved by that same call.
        profile: The requesting profile, needed for own-vote lookup, author
            masking, and nothing else.

    Returns:
        A JSON-serializable dict covering identity, description, dates,
        security, coordinates, boundary, aliases, links, community stats and
        counts, the article summary, and the comment count.
    """
    latitude = wiki.latitude
    longitude = wiki.longitude
    cover_photo = wiki.cover_photo

    payload: dict[str, Any] = {
        # The navigable identifier: wiki routes resolve a Location, so this is
        # what a client round-trips. wiki_slug is informational only.
        "location_slug": location.ensure_slug(),
        "wiki_slug": wiki.slug or None,
        "uuid": str(wiki.uuid),
        "name": wiki.name,
        "description": wiki.description or None,
        "pin_type": wiki.pin_type,
        "indoor_outdoor": wiki.indoor_outdoor or None,
        "date_abandoned": _isoformat_or_none(wiki.date_abandoned),
        "date_last_active": _isoformat_or_none(wiki.date_last_active),
        "security": {field: getattr(wiki, field) for field, _label in SECURITY_FIELDS},
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
        "address": wiki.address or None,
        "cover_photo_url": cover_photo.image.url if cover_photo is not None and cover_photo.image else None,
        "boundary": _boundary_geojson(wiki),
        # is_current comes from the shared helper rather than a local comparison
        # so this payload cannot drift from WikiAliasSerializer, which documents
        # the field, or from the delete guard, which decides what the rule means.
        "aliases": [{"id": alias.pk, "name": alias.name, "kind": alias.kind, "source": alias.source, "is_current": alias_is_current_name(alias, wiki)} for alias in wiki.aliases.all()],
        "links": [{"id": link.pk, "name": link.name, "url": link.url, "wayback_url": link.wayback_url or None, "order": link.order} for link in wiki.links.order_by("order", "pk")],
        "stats": _stats(wiki, profile),
        "article": _article_summary(wiki, profile),
        "comment_count": wiki.comments.count(),
        "created": _isoformat_or_none(wiki.created),
        "updated": _isoformat_or_none(wiki.updated),
    }

    # pin_count_low / pin_count_approx / first_pinned / first_pinned_precision.
    community = wiki_community_summary(wiki, location)
    payload.update(community)
    payload["first_pinned"] = _isoformat_or_none(community["first_pinned"])

    return payload
