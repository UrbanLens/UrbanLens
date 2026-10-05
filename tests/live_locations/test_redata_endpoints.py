"""Every notable campus has notable data on REData's endpoints.

Bounds and invariants only, never exact values: the sources change, and a test that pins a
count is a test that fails for no reason. Each check is one test per site, so a red names the
place and the kind of data that is missing. ``kirkbrides.toml``'s ``known_issues`` marks a
check that fails today for a tracked reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from live_sites import Answer, InconclusiveError, LiveRedata, Site
import pytest
from shapely.geometry import Point, shape

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

#: How far from the anchor an on-property building may be. A campus is large; a roster match
#: across town is the over-inclusion UrbanLens P148 describes.
_ON_PROPERTY_REACH_METERS = 1500

#: REData clamps every confidence into this range (docs/api-reference.md, boundaries).
_CONFIDENCE_RANGE = (0.01, 0.95)


def _point(site: Site) -> dict[str, float]:
    return {"lat": site.lat, "lng": site.lng}


def _parcel_uuid(redata: LiveRedata, site: Site) -> str:
    answer = redata.get("parcels/lookup/", **_point(site))
    if answer.status != 200 or not isinstance(answer.body, dict) or not answer.body.get("uuid"):
        pytest.fail(f"{site.name}: parcels/lookup answered {answer.status} {_error(answer)}")
    return str(answer.body["uuid"])


def _parcel(redata: LiveRedata, site: Site, facet: str) -> Answer:
    answer = redata.get(f"parcels/{_parcel_uuid(redata, site)}/{facet}/")
    assert answer.status == 200, f"{site.name}: parcels/{{uuid}}/{facet}/ answered {answer.status} {_error(answer)}"
    return answer


def _near(redata: LiveRedata, site: Site, path: str, **params: Any) -> Answer:
    answer = redata.get(path, **_point(site), **params)
    assert answer.status == 200, f"{site.name}: {path} answered {answer.status} {_error(answer)}"
    return answer


def _found(site: Site, answer: Answer, rows: list[dict[str, Any]], what: str) -> None:
    """Fail on an empty answer only when every covering source answered; otherwise nothing is known."""
    if rows:
        return
    if answer.unanswered_providers:
        raise InconclusiveError(f"{site.name}: no {what}, and {', '.join(answer.unanswered_providers)} did not answer")
    pytest.fail(f"{site.name}: no {what}")


def _error(answer: Answer) -> str:
    if isinstance(answer.body, dict):
        return f"{answer.body.get('error', '')}: {answer.body.get('message', '')}".strip(": ")
    return str(answer.body)[:200]


def _geometry(row: dict[str, Any]) -> BaseGeometry | None:
    raw = row.get("geometry")
    return shape(raw) if isinstance(raw, dict) and raw.get("type") else None


def _on_property_buildings(redata: LiveRedata, site: Site) -> list[dict[str, Any]]:
    return [row for row in _parcel(redata, site, "buildings").rows if row.get("is_on_property") is not False]


def _require_standing(site: Site) -> None:
    if not site.standing:
        pytest.skip(f"{site.name} is demolished; its buildings are gone")


def _relevant(site: Site, rows: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    return [row for row in rows if site.mentions(*(row.get(name) for name in fields))]


@pytest.mark.live_check("parcel")
def test_the_campus_resolves_to_a_parcel(redata: LiveRedata, site: Site) -> None:
    _parcel_uuid(redata, site)


@pytest.mark.live_check("boundary")
def test_one_suggested_boundary_holds_the_campus(redata: LiveRedata, site: Site) -> None:
    candidates = _parcel(redata, site, "boundaries").rows
    suggested = [row for row in candidates if row.get("is_suggested")]
    assert len(suggested) == 1, f"{site.name}: {len(suggested)} suggested boundaries among {len(candidates)}"
    winner = suggested[0]
    assert winner.get("kind") != "archaeological_buffer_area", (
        f"{site.name}: an archaeological buffer was suggested as the property"
    )
    geometry = _geometry(winner)
    assert geometry is not None and geometry.contains(Point(site.lng, site.lat)), (
        f"{site.name}: the suggested boundary ({winner.get('source')}) does not contain the campus point"
    )
    low, high = _CONFIDENCE_RANGE
    assert all(low <= float(row["confidence"]) <= high for row in candidates if row.get("confidence") is not None)


@pytest.mark.live_check("buildings")
def test_the_campus_has_its_buildings(redata: LiveRedata, site: Site) -> None:
    _require_standing(site)
    buildings = _on_property_buildings(redata, site)
    assert len(buildings) >= site.required_buildings, (
        f"{site.name}: {len(buildings)} buildings on the property, want at least {site.required_buildings}"
    )
    far = [row.get("name") for row in buildings if (row.get("distance_meters") or 0) > _ON_PROPERTY_REACH_METERS]
    assert not far, f"{site.name}: on-property buildings more than {_ON_PROPERTY_REACH_METERS} m away: {far[:5]}"


@pytest.mark.live_check("footprints")
def test_the_campus_buildings_have_footprints(redata: LiveRedata, site: Site) -> None:
    _require_standing(site)
    buildings = _on_property_buildings(redata, site)
    shaped = [
        row
        for row in buildings
        if (geometry := _geometry(row)) is not None and geometry.geom_type in {"Polygon", "MultiPolygon"}
    ]
    assert buildings, f"{site.name}: no buildings to outline"
    share = len(shaped) / len(buildings)
    assert share >= 0.8, (
        f"{site.name}: {len(shaped)} of {len(buildings)} on-property buildings have a footprint polygon"
    )


@pytest.mark.live_check("build_dates")
def test_the_campus_buildings_carry_build_dates(redata: LiveRedata, site: Site) -> None:
    _require_standing(site)
    dated = [row for row in _on_property_buildings(redata, site) if row.get("year_built")]
    assert dated, f"{site.name}: no on-property building carries a year_built"


@pytest.mark.live_check("ownership")
def test_the_parcel_has_an_owner_of_record(redata: LiveRedata, site: Site) -> None:
    owners = _parcel(redata, site, "owners").rows
    if owners:
        return
    assessments = _parcel(redata, site, "assessments").rows
    assert any(row.get("owner_name") or row.get("owner") for row in assessments), (
        f"{site.name}: no owner in owners/ or assessments/"
    )


@pytest.mark.live_check("register")
def test_a_historic_register_lists_the_campus(redata: LiveRedata, site: Site) -> None:
    rows = _near(redata, site, "cultural-resources/lookup/", radius_meters=500).rows
    listed = [
        row
        for row in rows
        if (site.nrhp and str(row.get("external_id", "")).startswith(site.nrhp)) or site.mentions(row.get("name"))
    ]
    assert listed, (
        f"{site.name}: none of {len(rows)} cultural resources within 500 m names the campus{f' or NRHP {site.nrhp}' if site.nrhp else ''}"
    )


@pytest.mark.live_check("wikipedia")
def test_the_campus_wikipedia_article_is_found_near_it(redata: LiveRedata, site: Site) -> None:
    rows = _near(redata, site, "reference-documents/", provider="wikipedia").rows
    titles = [row.get("title") for row in rows]
    assert site.wikipedia.lower() in {str(title).lower() for title in titles}, (
        f"{site.name}: Wikipedia near the campus gave {titles[:8]}, not {site.wikipedia!r}"
    )


@pytest.mark.live_check("documents")
def test_archives_hold_documents_about_the_campus(redata: LiveRedata, site: Site) -> None:
    answer = redata.get("reference-documents/search/", q=site.name)
    assert answer.status == 200, f"{site.name}: reference-documents/search answered {answer.status} {_error(answer)}"
    relevant = _relevant(site, answer.rows, "title", "description")
    assert relevant, f"{site.name}: none of {len(answer.rows)} archival results names the campus"


@pytest.mark.live_check("web_photos")
def test_a_web_image_search_finds_photos_of_the_campus(redata: LiveRedata, site: Site) -> None:
    answer = redata.get("search/web/", q=f'"{site.name}" {site.town}', images="true")
    assert answer.status == 200, f"{site.name}: search/web?images answered {answer.status} {_error(answer)}"
    relevant = _relevant(site, answer.rows, "title", "snippet", "link")
    assert len(relevant) >= 3, f"{site.name}: {len(relevant)} of {len(answer.rows)} image results name the campus"


@pytest.mark.live_check("web_search")
def test_a_web_search_finds_pages_about_the_campus(redata: LiveRedata, site: Site) -> None:
    answer = redata.get("search/web/", q=f'"{site.name}" {site.town}')
    assert answer.status == 200, f"{site.name}: search/web answered {answer.status} {_error(answer)}"
    assert _relevant(site, answer.rows, "title", "snippet", "link"), (
        f"{site.name}: none of {len(answer.rows)} web results names the campus"
    )


@pytest.mark.live_check("news")
def test_news_coverage_of_the_campus_is_found(redata: LiveRedata, site: Site) -> None:
    answer = redata.get("search/news/", q=f'"{site.wikipedia}"')
    assert answer.status == 200, f"{site.name}: search/news answered {answer.status} {_error(answer)}"
    assert _relevant(site, answer.rows, "title", "snippet", "description", "link", "url"), (
        f"{site.name}: none of {len(answer.rows)} news results names the campus"
    )


@pytest.mark.live_check("photos")
def test_photographs_of_the_campus_are_found(redata: LiveRedata, site: Site) -> None:
    answer = _near(redata, site, "media/lookup/", kind="photo")
    _found(site, answer, [row for row in answer.rows if row.get("kind") == "photo"], "photographs near the campus")


@pytest.mark.live_check("imagery")
def test_dated_aerial_imagery_of_the_campus_exists(redata: LiveRedata, site: Site) -> None:
    rows = _near(redata, site, "imagery/").rows
    dated = [row for row in rows if row.get("captured_on")]
    assert dated, f"{site.name}: {len(rows)} imagery layers, none dated"


@pytest.mark.live_check("historic_maps")
def test_historic_maps_cover_the_campus(redata: LiveRedata, site: Site) -> None:
    sheets = _near(redata, site, "maps/").rows
    volumes = _near(redata, site, "maps/volumes/").rows
    assert sheets or volumes, f"{site.name}: no historical map sheet or volume covers the campus"


@pytest.mark.live_check("incidents")
def test_incident_records_near_the_campus_exist(redata: LiveRedata, site: Site) -> None:
    answer = _near(redata, site, "incidents/", radius_meters=2000)
    _found(site, answer, answer.rows, "incident records within 2 km")
