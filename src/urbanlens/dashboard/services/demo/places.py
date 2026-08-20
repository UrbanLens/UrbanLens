"""The synthetic places the demo is built on.

Deliberately invented, not copied. The real database's locations are the one
thing this application exists to protect - it tracks ``LocationExposure``
provenance precisely because publishing an urbex site can get it demolished,
gated, or someone arrested - so seeding a public demo from real rows would be
the single largest exposure the system could produce. Every coordinate below is
a plausible-looking point in open country or on well-known public land, chosen
so the map, clustering, distance sorting and region grouping all have something
real-shaped to work on without naming a real site anybody is protecting.

Names are the kind of thing this app's users record, so the demo reads as the
product rather than as lorem ipsum, but no entry corresponds to an actual
location.
"""

from __future__ import annotations

from typing import Any

#: Synthetic places, spread across a few US regions so region grouping, "near
#: me" sorting and the map's clustering all have something to do. ``kind`` picks
#: the label set applied in seeding; ``blurb`` seeds the wiki article.
DEMO_PLACES: list[dict[str, Any]] = [
    {
        "name": "Hollow Creek Sanatorium",
        "latitude": 41.7392,
        "longitude": -73.9421,
        "kind": "hospital",
        "blurb": (
            "A tuberculosis sanatorium built in 1911 on the ridge above Hollow Creek, closed in 1974 when the "
            "county consolidated its facilities. Four connected wards, a boiler house and a detached morgue "
            "remain. The solarium's south wall is largely glass and is the reason most photographers come."
        ),
        "year_built": 1911,
    },
    {
        "name": "Ashgrove Textile Mill",
        "latitude": 42.1044,
        "longitude": -73.8812,
        "kind": "industrial",
        "blurb": (
            "Three-storey brick spinning mill on the Ashgrove race, running from 1876 until the company moved "
            "south in 1958. The turbine pit and a section of the headrace are intact. Partial roof collapse "
            "over the north bay happened some time in the 1990s."
        ),
        "year_built": 1876,
    },
    {
        "name": "Pinnacle Quarry Works",
        "latitude": 41.5510,
        "longitude": -74.1203,
        "kind": "industrial",
        "blurb": (
            "Bluestone quarry with an intact incline and two lime kilns cut into the hillside. Worked "
            "seasonally into the 1930s. The lower bench floods most springs and holds water well into summer."
        ),
        "year_built": 1889,
    },
    {
        "name": "Kestrel Field Airstrip",
        "latitude": 42.4471,
        "longitude": -73.7016,
        "kind": "military",
        "blurb": (
            "A wartime auxiliary field: one turf runway, a hangar with its original sliding door gear, and a "
            "concrete ops hut. Returned to pasture in 1949 and grazed ever since, which is why the hangar "
            "frame survived when the buildings around it did not."
        ),
        "year_built": 1942,
    },
    {
        "name": "Marrow Street Station",
        "latitude": 40.9812,
        "longitude": -73.8455,
        "kind": "transit",
        "blurb": (
            "A closed interurban stop, platform and stair head still in place behind hoarding. The tiled name "
            "band survives on the northbound wall. Track was lifted in 1961; the cut is now a drainage course."
        ),
        "year_built": 1908,
    },
    {
        "name": "Vantage Point Fire Tower",
        "latitude": 43.0219,
        "longitude": -74.4408,
        "kind": "tower",
        "blurb": (
            "Steel fire tower on state forest land, 60 feet to the cab floor. Decommissioned in 1988, stabilised "
            "by a volunteer group in 2009, and legal to climb - the one entry here that asks nothing of you but "
            "the walk in."
        ),
        "year_built": 1934,
    },
    {
        "name": "Old Fenwick Schoolhouse",
        "latitude": 41.8830,
        "longitude": -74.2657,
        "kind": "civic",
        "blurb": (
            "One-room schoolhouse, in use until district consolidation in 1953, then a grange hall, then empty. "
            "Blackboards, coat hooks and the stove flue are all still there. The roof is sound."
        ),
        "year_built": 1867,
    },
    {
        "name": "Sable Run Trestle",
        "latitude": 42.2698,
        "longitude": -74.0134,
        "kind": "bridge",
        "blurb": (
            "Deck plate girder trestle over Sable Run, 240 feet across and about 70 above the water. Rails "
            "lifted, ties mostly gone, stringers sound. Part of a rail trail proposal that has been three years "
            "from funding for about fifteen years."
        ),
        "year_built": 1901,
    },
    {
        "name": "Greywater Pumping Station",
        "latitude": 40.8944,
        "longitude": -73.9987,
        "kind": "industrial",
        "blurb": (
            "Municipal pumping station with two surviving triple-expansion engines under a glazed roof. The "
            "engine hall is the draw; the filter galleries below it flood and are not worth the trip."
        ),
        "year_built": 1893,
    },
    {
        "name": "Cardinal Drive-In",
        "latitude": 41.3388,
        "longitude": -73.6721,
        "kind": "leisure",
        "blurb": (
            "Screen tower, projection booth and about half the speaker posts, in a field that is mowed once a "
            "year by somebody. Last season was 1986. The tower's back face still carries the painted name."
        ),
        "year_built": 1951,
    },
    {
        "name": "Thorn Hill Estate",
        "latitude": 42.6605,
        "longitude": -73.9330,
        "kind": "residential",
        "blurb": (
            "Gilded-age house, twenty-odd rooms, vacant since a 1979 fire took the service wing. Main stair and "
            "the library's fitted shelving survive. Actively patrolled - included here because knowing a place "
            "is watched is part of planning a trip."
        ),
        "year_built": 1888,
    },
    {
        "name": "Lowland Grain Elevator",
        "latitude": 43.1177,
        "longitude": -73.5842,
        "kind": "industrial",
        "blurb": (
            "Concrete elevator on a spur off the main line, 1920s, with the headhouse and most of the leg intact. "
            "Visible for miles, which cuts both ways."
        ),
        "year_built": 1924,
    },
]

#: Places used to seed the *other* demo personas' pins, so friends' maps and the
#: activity feed are not just a copy of the login account's.
PERSONA_EXTRA_PLACES: list[dict[str, Any]] = [
    {"name": "Halden Colliery Fan House", "latitude": 41.4402, "longitude": -75.6620, "kind": "industrial", "blurb": "Ventilation fan house and a sealed drift mouth, all that is left above ground of the Halden workings.", "year_built": 1905},
    {"name": "Rill Valley Ice House", "latitude": 42.8817, "longitude": -73.7712, "kind": "industrial", "blurb": "Double-walled ice house on a pond that still freezes hard. Sawdust insulation is intact between the walls.", "year_built": 1898},
    {"name": "Beacon Hill Reservoir Gatehouse", "latitude": 41.6120, "longitude": -73.8005, "kind": "civic", "blurb": "Stone gatehouse over the outlet works, with the original valve gear and a spiral stair to the sluice deck.", "year_built": 1915},
]
