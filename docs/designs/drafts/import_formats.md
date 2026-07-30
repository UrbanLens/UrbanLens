Formats worth adding, roughly in priority order:

GPX — waypoints, tracks, routes with timestamps and elevation. Widely supported by hardware GPS units, hiking apps, and geocaching tools.
GeoJSON — the modern web-GIS lingua franca. If you ever want people to pull in data from OSM/Overpass queries, custom scripts, or other web mapping tools, this is the format they'll have.
Shapefile (.shp/.dbf/.shx bundle) — the GIS standard, especially for anything coming out of government/institutional sources (county GIS portals, EPA, state historic preservation offices). Given your interest in dump-site research and historical records, you'll likely run into this a lot.
WKT/WKB — less of a "site export" format, more useful if you want a lightweight way to let users paste a single geometry string.
OSM XML — output from Overpass Turbo queries. Handy if someone wants to pull "all abandoned buildings tagged in OSM within X radius" type data.

Sites/tools people commonly export from:

CalTopo — popular in the exploration/backcountry community, exports GPX, KML, and Shapefile. Given the field-exploration side of your interests, this one's probably underrated.
Gaia GPS — GPX/KML export, popular for offline trail/waypoint tracking.
Geocaching.com — GPX with Groundspeak extensions (custom XML namespace for cache metadata). If any of your users cross over into geocaching, this has real quirks worth handling explicitly.
Wikiloc / AllTrails — GPX exports (AllTrails gates it behind a paid tier).
Overpass Turbo / OpenStreetMap exports — GeoJSON or OSM XML, useful for auto-pulling candidate sites (e.g., tagged abandoned:* features).
Data.gov / state GIS portals / EPA Envirofacts — usually Shapefile, sometimes GeoJSON or CSV with lat/long columns. Relevant to your brownfield/dump-site research workflow specifically.
NRHP (National Register of Historic Places) GIS data — Shapefile/GeoJSON, could be a nice built-in dataset given your institutional-history focus rather than just an import format.