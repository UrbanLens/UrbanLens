"""Four views whose per-row cost is known, for exercising the endpoint mixin.

One well-behaved, and one deliberately broken on each axis
`EndpointScalingMixin` adds or composes. They live here rather than in the app
for the reason `urls_render_scaling.py` gives: mutating production code to make a
test fail proves nothing about production and leaves a trap for whoever reverts
it.

Each broken view is broken in exactly one way, so a harness test that expects
"objects/row" and gets "rows fetched/row" has found a bug in the instrument
rather than an ambiguity in the fixture.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.http import HttpResponse, JsonResponse
from django.urls import path

from urbanlens.dashboard.models.achievements.model import Achievement

if TYPE_CHECKING:
    from django.http import HttpRequest

#: Bytes of filler the wasteful view attaches to each row. Comfortably past
#: `MAX_BYTES_PER_ROW` so the axis trips on the payload rather than on noise.
FILLER_BYTES = 6_000

#: What the capped view will return however many rows exist.
PAYLOAD_CEILING = 3


def bounded(request: HttpRequest) -> JsonResponse:
    """A projection: no instances, one database row per rendered row, small.

    Args:
        request: The HTTP request.

    Returns:
        One small dict per achievement.
    """
    rows = list(Achievement.objects.order_by("pk").values("id", "name"))
    return JsonResponse({"rows": rows})


#: Extra in-memory instances the object-heavy view builds per row, on top of the
#: one the queryset itself constructs. Stands in for the companion objects a
#: `select_related` builds per row, and for the fresh related instance Django
#: builds per through-row of an m2m prefetch - the two things that turned 10,000
#: map pins into 63,240 model objects (R27). Four is over the 1.0 budget without
#: being so far over that the fixture stops resembling a real defect.
COMPANIONS_PER_ROW = 3


def builds_objects(request: HttpRequest) -> JsonResponse:
    """Reads the same data through model instances, with companions per row.

    Deliberately builds its companions in memory rather than by querying: this
    view must break exactly one axis, and a per-row query would trip the
    statement counter too and make the harness ambiguous about which axis caught
    it.

    Args:
        request: The HTTP request.

    Returns:
        The same payload as :func:`bounded`, built the expensive way.
    """
    rows = []
    for row in Achievement.objects.order_by("pk"):
        # Each of these fires post_init, which is what the axis counts.
        companions = [Achievement(name=row.name, metric=row.metric) for _ in range(COMPANIONS_PER_ROW)]
        rows.append({"id": row.pk, "name": companions[-1].name})
    return JsonResponse({"rows": rows})


def fetches_every_row(request: HttpRequest) -> JsonResponse:
    """Counts by dragging every row into Python, in one flat statement.

    The shape `QueryScalingMixin` is blind to: the query count never moves,
    because there is only ever one query. `SavedFilterMatchCountsView` does this
    with pin uuids.

    Args:
        request: The HTTP request.

    Returns:
        A constant-size body, whatever the row count.
    """
    ids = list(Achievement.objects.values_list("id", flat=True))
    return JsonResponse({"total": len(ids)})


def ships_too_much(request: HttpRequest) -> JsonResponse:
    """Attaches several kilobytes of filler to every row.

    Args:
        request: The HTTP request.

    Returns:
        A payload whose size is set by the row count, generously.
    """
    filler = "x" * FILLER_BYTES
    rows = [{"id": row["id"], "blob": filler} for row in Achievement.objects.order_by("pk").values("id")]
    return JsonResponse({"rows": rows})


def capped(request: HttpRequest) -> HttpResponse:
    """Bounded on every per-row axis *and* on the total, which is the point.

    A per-row budget cannot express "and never more than N records"; this is what
    the ceiling axis reads.

    Args:
        request: The HTTP request.

    Returns:
        At most :data:`PAYLOAD_CEILING` records, with a truncation marker.
    """
    rows = list(Achievement.objects.order_by("pk").values("id", "name")[: PAYLOAD_CEILING + 1])
    truncated = len(rows) > PAYLOAD_CEILING
    body = {"rows": rows[:PAYLOAD_CEILING], "truncated": truncated}
    return HttpResponse(json.dumps(body), content_type="application/json")


def uncapped(request: HttpRequest) -> HttpResponse:
    """The same shape with no ceiling - what the ceiling axis must catch.

    Args:
        request: The HTTP request.

    Returns:
        Every record, with a marker that always says complete.
    """
    rows = list(Achievement.objects.order_by("pk").values("id", "name"))
    return HttpResponse(json.dumps({"rows": rows, "truncated": False}), content_type="application/json")


urlpatterns = [
    path("bounded/", bounded, name="endpoint_scaling.bounded"),
    path("objects/", builds_objects, name="endpoint_scaling.objects"),
    path("rows/", fetches_every_row, name="endpoint_scaling.rows"),
    path("bytes/", ships_too_much, name="endpoint_scaling.bytes"),
    path("capped/", capped, name="endpoint_scaling.capped"),
    path("uncapped/", uncapped, name="endpoint_scaling.uncapped"),
]
