# D20 — A place's automatic name is chosen by what kind of name it is before who said it

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D20` · `status: accepted` · `updated: 2026-09-23` · `asked for by: Jess, 2026-09-23 (HRSH courtyard pin, P145)` · `code: src/urbanlens/dashboard/services/locations/name_tiers.py`

## Decision

Every automatic name candidate carries a **tier**, and the tier decides first. Best first:

| Tier | What it is | Sources today |
| --- | --- | --- |
| `ENCYCLOPEDIA` | The Wikipedia article matched to the place | `wikipedia` |
| `HISTORIC_REGISTER` | A register listing (NRHP, a state register) **whose own boundary contains the point** | `historic_register` (`services/locations/register_names.py`): REData register rows and CRIS's site record, each flagged `contains_point` when fetched |
| `SITE` | A named site, campus, park or landuse area the point is in | `nps`; `nominatim` when it named an areal amenity/landuse/leisure/historic/tourism object |
| `BUILDING` | One building's name | `cris` (USN name), `redata_building`; `nominatim` when it named a building |
| `POI` | A point of interest near the point | `google_places`, `azure_maps`, `epa_echo`; a `nominatim` node; any source not listed |
| `ROAD` | A road or street address | `nominatim` when it named a highway-family way or an address |

Within a tier the old rules still apply: agreement between sources, then the admin's
**Name source priority**, then arrival order. The admin setting therefore orders sources *within* a tier
and can no longer lift a worse tier over a better one.

Three rules sit on top of the ranking:

1. **A road or address name never names a place** - not the Location's `official_name`, not its wiki's
   title, not an alias. A road name already in place is retired when nothing replaces it: the official name
   is cleared and an automatically named wiki returns to its placeholder.
2. **A building's name names or aliases a property only when the property is known to hold exactly one
   building and the named building is on it.** Without a parcel, a building found within a search radius
   says nothing about the property; on a campus, one building's name is not the campus's. A building's own
   page (a Location holding only child pins, or a child wiki's) ranks `BUILDING` first.
3. **An automatic name gives way to a strictly better tier; a name a person wrote never does.** "Automatic"
   is `wiki_naming.name_set_by_person` being false. The current name's tier is the best tier among the
   sources of its matching aliases. A name whose alias comes from anything but a ranked naming source (a
   `user` alias left by a system or legacy write) is not outranked.

A second person's root pin elsewhere on the property has a Location of its own, with caches that may never
have fetched the register or the article. Naming it also draws the shared wiki's official aliases of `SITE`
tier or better, and a register listing arriving at one Location re-names the property's other root-pin
Locations (`models/cache/signals.refresh_names_on_register_listing`).

Official aliases automation added that these rules no longer admit are pruned from the wiki and the
location's pins. A person's alias (`source="user"`) is never pruned, and nothing is tombstoned, so a
building's name comes back if its parcel turns out to hold only that building.

## Rationale

On k3s-staging the HRSH courtyard pin (41.73266, -73.92736, P145) was titled **"Courtyard Drive"**. That
was Nominatim's reverse geocode: the smallest OSM object under the point, a private service road
(way/352353227). It won because `nominatim` is first in the default priority and nothing distinguished a
road from a place. The same name was the Location's `official_name`, which `Pin.get_unique_search_name`
prefers, so the Wikipedia, Wikimedia, Smithsonian and GDELT lookups all searched for the road.

Jess asked for the NRHP title to win over less detailed ones, with a metric that makes the rule explicit,
and then ruled (2026-09-23) that the matched Wikipedia article wins over the NRHP listing: a listing can
name one building on a larger plot ("Hudson River State Hospital, Main Building"), where the article names
the property. The ranking otherwise follows how specifically each kind of name identifies a *property*. A site polygon is a name someone drew around the place. A building names
part of it, a POI names something near it, and a road names what runs past it.

**Containment, not presence, admits a register name.** REData's National Register answer at the
requirement pin can be only the Isaac Roosevelt House, a point listing about 550 m away
(measured 2026-09-23). Presence in the list would have named the campus after it.

## Consequences and open questions

- The HRSH campus is titled **"Hudson River State Hospital"** (its Wikipedia article); the listing's
  "Hudson River State Hospital, Main Building" is an alias. A wiki automatically named after its listing
  is renamed when the article arrives.
- Cached register and CRIS rows fetched before 2026-09-23 have no `contains_point`, so they name nothing
  until they refresh.
- `test_name_resolution.py::test_admin_priority_orders_lone_sources` asserted that the admin priority could
  put `nps` over `wikipedia`. It now asserts ordering within a tier, and a new test asserts that a tier beats
  the priority.
