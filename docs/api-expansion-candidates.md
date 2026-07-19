# API Expansion Candidates for UrbanLens

This document inventories external APIs implemented under `dashboard/services/apis/`, lists additional candidates for expansion and redundancy, and notes **cost** and **licensing** for each.

---

## Legend

### Cost

| Label | Meaning |
|---|---|
| **Free** | No meaningful usage cost for our scale; public APIs, open datasets, or self-hosted OSS with no license fee. |
| **Low cost** | Paid or metered, but usable on a free tier, modest subscription, or pay-per-call that stays affordable at UrbanLens scale. |
| **High cost** | Enterprise pricing, expensive per-seat/per-call rates, or imagery/data licensing that only makes sense for pro/ commercial tiers. |

Costs change - verify current pricing before integrating.

### License / model

| Label | Meaning |
|---|---|
| **Open source** | Self-hostable open-source software and/or code you can run yourself (OSRM, Pelias, SearXNG, etc.). |
| **Open data** | Free APIs or datasets under open or public-domain terms (OSM ODbL, US government works, CC0/CC-BY archives). The *service* may still be operated by a third party. |
| **Proprietary** | Commercial closed service; terms of use restrict scraping, redistribution, or self-hosting. |

Many entries are **Open data** served via a **Proprietary** platform (e.g. Esri-hosted public layers). Both columns apply when relevant.

---

## What We Already Have

| Category | Implemented | Cost | License | Notes |
|---|---|---|---|---|
| **Geocoding / places** | Google Geocoding | Low cost | Proprietary | $200/mo Google credit; primary geocoder |
| | Google Places (legacy + v1) | Low cost | Proprietary | Nearby search, place details |
| | Nominatim (OSM) | Free | Open source + Open data | 1 req/s public instance limit |
| | Apple Maps Server API | Low cost | Proprietary | Implemented; not wired in `pin.py` |
| **Satellite / aerial** | Google Maps Static | Low cost | Proprietary | Satellite carousel |
| | Esri Wayback / basemaps | Free | Open data + Proprietary platform | Public ArcGIS REST endpoints |
| | NASA GIBS | Free | Open data | Global satellite browse |
| | Mapbox Static | Low cost | Proprietary | Free tier available |
| | Bing Maps aerial | Low cost | Proprietary | Azure key required |
| | OpenAerialMap | Free | Open data | Metadata + open imagery links |
| | USGS topo / M2M | Free | Open data | Historical topo, EarthExplorer |
| **Street-level** | Google Street View | Low cost | Proprietary | Static/pano metadata |
| | Mapillary | Free / Low cost | Proprietary (Meta) | Free dev tier |
| | KartaView | Free | Open data | Crowdsourced; formerly OpenStreetCam |
| **Boundaries** | Overpass (OSM) | Free | Open source + Open data | First in boundary chain |
| | Regrid parcels | Low cost / High cost | Proprietary | Paid parcel API |
| | Overture Maps | Free | Open data | GeoParquet building footprints |
| | Microsoft Building Footprints | Free | Open data | Global ML footprints |
| | Google Open Buildings | Free | Open data | V3 footprint dataset |
| **Historical maps** | OpenHistoricalMap | Free | Open source + Open data | Historic OSM fork |
| | Wayback Machine | Free | Open source + Open data | IA CDX, memento, snapshots |
| | USGS historical topo | Free | Open data | Already in USGS gateway |
| | Google Earth (Engine + Web) | Free / Low cost | Proprietary | OAuth for EE REST; web links free |
| **Archives / media** | Wikipedia | Free | Open data | CC-BY-SA content |
| | Wikimedia Commons | Free | Open data | Free media search |
| | Smithsonian Open Access | Free | Open data | US-centric; API key required |
| | Library of Congress | Free | Open data | `loc.gov/api` |
| | Digital Commonwealth | Free | Open data | Massachusetts archive |
| **Search** | Google Custom Search | Low cost | Proprietary | 100 queries/day free tier |
| | Brave Search | Low cost | Proprietary | ~2k queries/month free |
| | News (stub) | - | - | Not fully implemented |
| **Weather** | OpenWeatherMap | Free / Low cost | Proprietary | 1k calls/day free tier |
| **Parks / civic** | NPS API | Free | Open data | US national parks |
| | NPS ArcGIS boundaries | Free | Open data | FeatureServer queries |
| | Data.gov (CT sales) | Free | Open data | Connecticut-only sample |
| **Real estate** | LoopNet | Free (scrape) | Proprietary | Fragile; ToS risk |
| **Routing** | RouteXL | Low cost | Proprietary | Exists; lightly integrated |
| **AI** | OpenAI | Low cost | Proprietary | Usage-based |
| | Cloudflare AI | Low cost | Proprietary | Worker AI endpoint |
| | HuggingFace | Free / Low cost | Proprietary platform + Open source models | Inference API + OSS weights |
| **Other** | GitHub contributors | Free | Proprietary | Thanks page only |
| | Google Location History import | Free | Proprietary | User-owned Takeout JSON |

---

## APIs Worth Considering

Grouped by capability. **Cost** and **License** columns added for every entry.

---

### Geocoding, Search & Place Metadata

| API | Cost | License | Why consider it |
|---|---|---|---|
| [OpenCage](https://opencagedata.com/) | Low cost | Proprietary | OSM-backed geocoder; higher limits than public Nominatim |
| [LocationIQ](https://locationiq.com/) | Free / Low cost | Proprietary + Open data | OSM-based; generous free tier |
| [Geoapify](https://www.geoapify.com/) | Free / Low cost | Proprietary + Open data | Geocoding, places, routing bundle |
| [Pelias](https://www.pelias.io/) (self-host) | Free | Open source | Composable geocoder; blend OSM + WOF |
| [Pelias / Geocode Earth](https://geocode.earth/) | Low cost | Proprietary + Open data | Hosted Pelias |
| [Photon](https://photon.komoot.io/) (Komoot) | Free | Open source + Open data | Fast OSM geocoder; Komoot public instance |
| [HERE Geocoding & Search](https://developer.here.com/) | Free / Low cost | Proprietary | Freemium tier; global coverage |
| [Mapbox Geocoding](https://docs.mapbox.com/api/search/geocoding/) | Low cost | Proprietary | Consolidate with existing Mapbox key |
| [TomTom Search](https://developer.tomtom.com/) | Low cost | Proprietary | Another major vendor fallback |
| [Foursquare Places](https://location.foursquare.com/) | Low cost | Proprietary | Rich POI metadata and categories |
| [Yelp Fusion](https://www.yelp.com/developers/documentation/v3) | Free / Low cost | Proprietary | Business status and photos |
| [GeoNames](http://www.geonames.org/export/web-services.html) | Free | Open data | Gazetteer; population, elevation, alt names |
| [Wikidata Query Service](https://query.wikidata.org/) | Free | Open data | Structured place facts; SPARQL |
| [Who's On First (WOF)](https://whosonfirst.org/) | Free | Open data | Foursquare open gazetteer; campus disambiguation |
| [Placekey](https://www.placekey.io/) | Free / Low cost | Proprietary | Stable cross-dataset place IDs |
| [What3Words](https://developer.what3words.com/) | Low cost | Proprietary | Remote sites without street addresses |
| [libpostal](https://github.com/openvenues/libpostal) | Free | Open source | Address parsing/normalization (self-host) |
| [Natural Earth](https://www.naturalearthdata.com/) | Free | Open data | Admin boundaries, populated places (static GeoJSON) |

**Redundancy gap:** Heavy Google reliance; Nominatim is the only free geocoding fallback at 1 req/s.

---

### Satellite, Aerial & Temporal Imagery

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Sentinel Hub / Copernicus](https://www.sentinel-hub.com/) | Free / Low cost | Proprietary platform + Open data | Sentinel-2 time series |
| [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) | Free | Open data | EU Sentinel/Landsat access without middleman |
| [Element84 Earth Search (STAC)](https://earth-search.aws.element84.com/v1) | Free | Open source + Open data | STAC catalog on AWS Open Data |
| [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) | Free | Proprietary platform + Open data | Hosted Landsat, NAIP, Sentinel |
| [USDA NAIP](https://www.fsa.usda.gov/programs-and-services/aerial-photography/imagery-programs/naip-imagery/) | Free | Open data | ~1 m US aerial orthophotos |
| [Nearmap](https://www.nearmap.com/au/en/products/api) | High cost | Proprietary | Sub-decimeter oblique/vertical |
| [Maxar / SecureWatch](https://www.maxar.com/products/satellite-imagery) | High cost | Proprietary | Premium tasking and archive |
| [SkyFi](https://skyfi.com/) | High cost | Proprietary | On-demand satellite marketplace |
| [Historic Aerials](https://www.historicaerials.com/) | Low cost | Proprietary | US historical aerial mosaics |
| [NOAA Coastal Imagery](https://coast.noaa.gov/) | Free | Open data | Coastal sites, piers, forts |
| [IGN Géoportail](https://geoservices.ign.fr/) | Free / Low cost | Open data + Proprietary platform | French orthophotos and historic layers |
| [Landsat Look / GLOVIS](https://landsatlook.usgs.gov/) | Free | Open data | USGS browse beyond existing M2M gateway |
| [Sentinel-2 via Google Earth Engine](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR) | Free / Low cost | Proprietary platform + Open data | Time series if EE OAuth configured |
| [OpenTopoMap](https://opentopomap.org/) | Free | Open source + Open data | Topographic raster tiles (SRTM + OSM) |
| [Protomaps](https://protomaps.com/) | Free | Open source | PMTiles basemaps; self-host or CDN |
| [OpenMapTiles](https://openmaptiles.org/) | Free | Open source + Open data | Vector tile schema from OSM |
| [MODIS / VIIRS (NASA LP DAAC)](https://lpdaac.usgs.gov/) | Free | Open data | Coarse global time series, fire, night lights |

**Redundancy gap:** Strong US public-sector coverage; weaker global high-res and programmatic time-series beyond Esri Wayback tiles.

---

### Street-Level & Immersive Imagery

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Bing Streetside](https://learn.microsoft.com/en-us/bingmaps/rest-services/) | Low cost | Proprietary | Pairs with Bing aerial gateway |
| [Panoramax](https://panoramax.fr/) | Free | Open source + Open data | EU open street-level imagery (IGN) |
| [OpenStreetCam / KartaView API](https://kartaview.org/) | Free | Open data | Already integrated; ensure full API surface used |
| [Cyclomedia](https://www.cyclomedia.com/) | High cost | Proprietary | Dense NL/EU/US metro coverage |
| [Google Photorealistic 3D Tiles](https://developers.google.com/maps/documentation/tile/3d-tiles) | Low cost | Proprietary | 3D campuses and industrial complexes |
| [Cesium ion](https://cesium.com/platform/cesium-ion/) | Free / Low cost | Proprietary + Open source | CesiumJS OSS; ion hosts terrain/3D tiles |
| [OpenDroneMap](https://www.opendronemap.org/) | Free | Open source | User-uploaded photogrammetry (self-host) |

**Redundancy gap:** Three providers is solid; Bing Streetside and Panoramax fill geographic holes.

---

### Building Footprints, Parcels & Land Boundaries

| API | Cost | License | Why consider it |
|---|---|---|---|
| [LightBox Parcel API](https://www.lightboxre.com/) | Low cost / High cost | Proprietary | Regrid alternative |
| [ATTOM Data](https://api.developer.attomdata.com/) | Low cost / High cost | Proprietary | Property, foreclosure, ownership |
| [CoreLogic / Cotality](https://www.corelogic.com/) | High cost | Proprietary | Enterprise parcel data |
| [OpenAddresses](https://openaddresses.io/) | Free | Open data | Global address points |
| [US Census TIGER/Line](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html) | Free | Open data | Parcels, roads, block groups (US) |
| [FEMA NFHL (flood zones)](https://www.fema.gov/flood-maps/national-flood-hazard-layer) | Free | Open data | Coastal/industrial flood context |
| [BLM PLSS / public lands](https://www.blm.gov/services/geospatial-data) | Free | Open data | Western US mines, ghost towns, federal land |
| [USFS MVUM / recreation](https://data.fs.usda.gov/geodata/) | Free | Open data | Trails, cabins, fire lookouts |
| OSM `abandoned:` / `disused:` via Overpass | Free | Open source + Open data | Extend existing Overpass queries |
| NYC / Boston 3D CityGML | Free | Open data | Municipal open building models |
| [Global Building Atlas](https://www.globalbuildingatlas.org/) | Free | Open data | Research-grade global footprints |

**Redundancy gap:** Regrid is paid and proprietary; TIGER + OpenAddresses + Overture improve free resilience.

---

### Heritage, Abandonment & Urbex Signal Data

| API | Cost | License | Why consider it |
|---|---|---|---|
| [NPS NRHP](https://irma.nps.gov/DataStore/) | Free | Open data | National Register of Historic Places |
| [State SHPO datasets](https://www.nps.gov/subjects/nationalregister/data-downloads.htm) | Free | Open data | State historic inventories |
| [EPA ECHO / Superfund](https://echo.epa.gov/tools/data-downloads) | Free | Open data | Industrial contamination sites |
| [EPA Brownfields](https://www.epa.gov/brownfields/brownfields-and-land-revitalization-data-and-reports) | Free | Open data | Redevelopment pipeline |
| [RCRAInfo / EPA Envirofacts](https://echo.epa.gov/tools/data-downloads) | Free | Open data | Hazardous waste handlers, facilities |
| [USGS MRDS](https://mrdata.usgs.gov/mrds/) | Free | Open data | Mines, quarries, smelters |
| [OSMRE Abandoned Mine Lands](https://amlis.osmre.gov/) | Free | Open data | US AML locations |
| [HAER/HABS (LOC)](https://www.loc.gov/pictures/collection/hh/) | Free | Open data | Historic American Buildings Survey |
| [UNESCO World Heritage](https://whc.unesco.org/en/list/) | Free | Open data | Global heritage sites (bulk downloads) |
| [Atlas Obscura](https://www.atlasobscura.com/) | Free (scrape) | Proprietary | No official API; unusual places |
| OSM `ruins`, `brownfield`, `industrial` tags | Free | Open data | Urbex-specific Overpass enrichment |
| [Socrata demolition/permit APIs](https://dev.socrata.com/) | Free | Open data | City open data portals |
| [FCC ASR (towers)](https://www.fcc.gov/general/antenna-structure-registration-asr) | Free | Open data | Antenna/tower registrations |
| Municipal vacancy registries | Free | Open data | Detroit, Baltimore, etc. |
| [OpenInfraMap](https://openinframap.org/) | Free | Open source + Open data | Power lines, pipelines, telecom - industrial context |
| [OpenRailwayMap](https://www.openrailwaymap.org/) | Free | Open source + Open data | Abandoned rail corridors and yards |

---

### Archives, Photos & Historical Context

| API | Cost | License | Why consider it |
|---|---|---|---|
| [DPLA](https://pro.dp.la/developers/api-codex) | Free | Open data | National US archive aggregation |
| [Europeana](https://pro.europeana.eu/page/apis) | Free | Open data | European photos, maps, texts |
| [NYPL Digital Collections API](https://api.repo.nypl.org/) | Free | Open data | Urban photography and maps |
| [Getty Open Content](https://www.getty.edu/about/what-we-do/open-content.html) | Free | Open data | Architectural and art history |
| [Flickr API](https://www.flickr.com/services/api/) | Free / Low cost | Proprietary | Geotagged historical photo groups |
| [Historypin](https://www.historypin.org/) | Free | Proprietary | Community historical photos on maps |
| [Old Maps Online](https://www.oldmapsonline.org/) | Free | Open data + Proprietary UI | Historical map discovery |
| [Calisphere](https://calisphere.org/) | Free | Open data | California digital collections |
| [Trove (NLA Australia)](https://trove.nla.gov.au/about/create-something/using-api) | Free | Open data | Australian newspapers and photos |
| [Gallica (BnF)](https://api.bnf.fr/) | Free | Open data | French national library |
| [National Archives Catalog API](https://catalog.archives.gov/api/v1/) | Free | Open data | US government records |
| [Internet Archive APIs](https://archive.org/services/docs/api/) | Free | Open source + Open data | Books, images, texts beyond Wayback |
| [Met Museum Open Access](https://metmuseum.github.io/) | Free | Open data | CC0 collection API |
| [Openverse](https://openverse.org/) | Free | Open source + Open data | CC-licensed media search (WordPress) |
| [BHL (Biodiversity Heritage Library)](https://www.biodiversitylibrary.org/) | Free | Open data | Historical botanical/industrial illustrations |

---

### Weather, Light & Shooting Conditions

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | Free | Open data | No API key; OWM redundancy |
| [NOAA/NWS API](https://www.weather.gov/documentation/services-web-api) | Free | Open data | US forecasts, alerts, observations |
| [Meteostat](https://meteostat.net/) | Free | Open data | Historical weather observations JSON API |
| [Visual Crossing](https://www.visualcrossing.com/weather-api) | Free / Low cost | Proprietary | Historical weather for photo dates |
| [Tomorrow.io](https://www.tomorrow.io/weather-api/) | Free / Low cost | Proprietary | Hyperlocal forecasts |
| [Sunrise-Sunset.org](https://sunrise-sunset.org/api) | Free | Open data | Golden/blue hour planning |
| [SunCalc](https://github.com/mourner/suncalc) | Free | Open source | Solar position library (self-host in backend) |
| [PurpleAir](https://develop.purpleair.com/) | Free | Proprietary + Open data | Community air quality sensors |
| [NASA VIIRS Black Marble](https://earth.gov/ghgcenter/data-catalog/nasa-viirs-black-marble) | Free | Open data | Night lights / light pollution |
| [Light Pollution Map (API)](https://www.lightpollutionmap.info/) | Free | Proprietary | Night photography planning |
| [World Tides](https://www.worldtides.info/) | Low cost | Proprietary | Coastal access windows |
| [USGS streamflow](https://waterservices.usgs.gov/) | Free | Open data | Flood risk for tunnels and culverts |

---

### Parks, Recreation & Access

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Recreation.gov RIDB](https://ridb.recreation.gov/) | Free | Open data | Campgrounds, permits, closures |
| [USFS Recreation](https://www.fs.usda.gov/) | Free | Open data | Trails, cabins, fire lookouts |
| AllTrails | - | Proprietary | No public API |
| OSM trails via Overpass | Free | Open source + Open data | Hiking/forest access paths |
| State park GIS REST endpoints | Free | Open data | Same pattern as NPS ArcGIS gateway |
| [Peakbagger](https://www.peakbagger.com/) | Free | Proprietary | Peak lists and coordinates (scrape/export) |
| [iNaturalist API](https://api.inaturalist.org/v1/docs/) | Free | Open source + Open data | Observations at coordinates |
| [eBird API](https://ebird.org/api/v2/ref/hotspot/) | Free | Open data | Hotspot locations |
| [GBIF Occurrence API](https://www.gbif.org/developer/occurrence) | Free | Open data | Biodiversity records |
| [OurAirports](https://ourairports.com/data/) | Free | Open data | Abandoned airfields and small airports |
| [OpenTripPlanner](https://www.opentripplanner.org/) | Free | Open source | Multimodal transit + walk access (self-host) |

---

### Routing, Directions & Trip Optimization

| API | Cost | License | Why consider it |
|---|---|---|---|
| [OpenRouteService](https://openrouteservice.org/) | Free / Low cost | Open source + Proprietary hosted | Routing, isochrones, matrix |
| [GraphHopper](https://www.graphhopper.com/) | Free / Low cost | Open source + Proprietary hosted | Self-host OSS edition for free |
| [OSRM](http://project-osrm.org/) | Free | Open source | Fast routing on OSM (self-host) |
| [Valhalla](https://github.com/valhalla/valhalla) | Free | Open source | Multimodal routing (self-host) |
| [BRouter](https://brouter.de/) | Free | Open source | Hiking/cycling/off-road profiles |
| [Mapbox Directions](https://docs.mapbox.com/api/navigation/) | Low cost | Proprietary | Consolidate with Mapbox key |
| [Google Routes API](https://developers.google.com/maps/documentation/routes) | Low cost | Proprietary | Premium fallback |
| [HERE Routing](https://developer.here.com/products/routing) | Free / Low cost | Proprietary | Rural/truck routing |
| OptimoRoute / Routific | High cost | Proprietary | Fleet optimization at scale |

---

### Web Search, News & Social Context

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Bing Web Search API](https://www.microsoft.com/en-us/bing/apis/bing-web-search-api) | Low cost | Proprietary | Third search fallback |
| [SerpAPI / Zenserp](https://serpapi.com/) | Low cost | Proprietary | Meta-search when direct APIs fail |
| [GDELT Project](https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/) | Free | Open data | Geocoded global news events |
| [Common Crawl](https://commoncrawl.org/) | Free | Open data | Web crawl archives for research |
| [SearXNG](https://github.com/searxng/searxng) | Free | Open source | Self-hosted meta-search |
| [Eventbrite API](https://www.eventbrite.com/platform/api/) | Free / Low cost | Proprietary | Photo walks near a pin |
| [Reddit API](https://www.reddit.com/dev/api/) | Free / Low cost | Proprietary | Location threads (strict ToS) |
| Mastodon REST / ActivityPub | Free | Open source | Fediverse photo communities |
| [NewsAPI.org](https://newsapi.org/) | Free / Low cost | Proprietary | Replace stub `NewsGateway` |
| [Tavily / Exa / Perplexity](https://tavily.com/) | Low cost | Proprietary | AI-native location research |

---

### Elevation, Terrain & 3D Context

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Open-Elevation](https://open-elevation.com/) | Free | Open source + Open data | Public DEM API; self-hostable |
| [USGS EPQS](https://apps.nationalmap.gov/epqs/) | Free | Open data | Authoritative US point elevation |
| [Copernicus DEM / SRTM](https://spacedata.copernicus.eu/) | Free | Open data | Global DEM downloads |
| [Mapzen Terrarium / Terrarium tiles](https://github.com/tilezen/joerd) | Free | Open source + Open data | RGB-encoded DEM tiles |
| [Mapbox Terrain-RGB](https://docs.mapbox.com/data/tilesets/reference/mapbox-terrain-rgb-v1/) | Low cost | Proprietary | Terrain shading |
| [Cesium World Terrain](https://cesium.com/platform/cesium-ion/content/cesium-world-terrain/) | Free / Low cost | Proprietary | 3D terrain meshes |
| [USGS 3DEP / OpenTopography](https://opentopography.org/) | Free | Open data | Lidar point clouds |
| [Google Elevation API](https://developers.google.com/maps/documentation/elevation) | Low cost | Proprietary | Simple fallback |

---

### Real Estate & Property Lifecycle

| API | Cost | License | Why consider it |
|---|---|---|---|
| [ATTOM Property API](https://api.developer.attomdata.com/docs) | Low cost / High cost | Proprietary | Foreclosure, ownership |
| [Estated](https://estated.com/) | Low cost | Proprietary | Property records |
| [Crexi](https://www.crexi.com/) | Free (browse) | Proprietary | Commercial RE; no clean API |
| Zillow / Redfin | - | Proprietary | No open API for listings |
| [Realtor.com API](https://developer.realtor.com/) | Low cost | Proprietary | Listing history |
| County assessor GIS REST | Free | Open data | Owner, year built, last sale |
| Overture / Microsoft `year_built` enrichment | Free | Open data | Extend existing footprint pipeline |

---

### AI & Enrichment

| API | Cost | License | Why consider it |
|---|---|---|---|
| [Anthropic Claude](https://docs.anthropic.com/) | Low cost | Proprietary | LLM redundancy |
| [Google Gemini](https://ai.google.dev/) | Free / Low cost | Proprietary | Pairs with Google stack |
| [Groq / Together / Fireworks](https://groq.com/) | Free / Low cost | Proprietary | Fast inference |
| [Azure AI Search](https://azure.microsoft.com/en-us/products/ai-services/ai-search/) | Low cost | Proprietary | RAG over wiki + API results |
| [Google Cloud Vision](https://cloud.google.com/vision) | Low cost | Proprietary | Photo auto-tagging |
| [Ollama](https://ollama.com/) | Free | Open source | Local LLM/vision models |
| CLIP / LLaVA via HuggingFace | Free / Low cost | Open source models | Extend existing HF gateway |

---

### Safety, Legal & Responsibility

| API | Cost | License | Why consider it |
|---|---|---|---|
| [OpenFEMA](https://www.fema.gov/about/openfema) | Free | Open data | Disasters, declarations, assistance |
| [USGS Earthquake Hazards](https://earthquake.usgs.gov/fdsnws/event/1/) | Free | Open data | Seismic risk near structures |
| [USGS Landslide hazards](https://www.usgs.gov/programs/landslide-hazards) | Free | Open data | Slope failure context |
| [IPAWS / NWS alerts](https://alerts-v2.weather.gov/) | Free | Open data | Real-time weather emergencies |
| [SpotCrime](https://spotcrime.com/) | Free / Low cost | Proprietary | Crime near trip stops (sensitive UX) |
| City open crime data (Socrata) | Free | Open data | Local incident feeds |
| County parcel owner (assessor GIS) | Free | Open data | Trespassing / permission context |

---

## Additional Free & Open Source APIs

Extra candidates that are **Free** cost and **Open source** and/or **Open data**, prioritized for UrbanLens. Many are not yet listed above.

### Geospatial infrastructure (self-host or public instances)

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) | Free | Open source + Open data | Already used; add private mirror for rate-limit headroom |
| [tile.openstreetmap.org](https://operations.osmfoundation.org/policies/tiles/) | Free | Open data | Basemap fallback (respect OSMF tile policy) |
| [OpenStreetMap taginfo API](https://taginfo.openstreetmap.org/) | Free | Open source + Open data | Discover urbex-relevant tags (`abandoned=*`, `ruins=*`) |
| [OSM Wikidata tagfinder](https://tagfinder.hotosm.org/) | Free | Open source + Open data | Tag suggestion for mappers and wiki editors |
| [Nominatim (self-hosted)](https://nominatim.org/) | Free | Open source + Open data | Remove 1 req/s public limit |
| [Pelias](https://github.com/pelias/pelias) | Free | Open source + Open data | Full geocoder stack on OSM + WOF |
| [Martin / pg_tileserv](https://github.com/maplibre/martin) | Free | Open source | Serve PostGIS campus/location tiles from our DB |
| [MapLibre GL](https://maplibre.org/) | Free | Open source | OSS map renderer (no API, but replaces proprietary map SDKs) |
| [STAC FastAPI](https://github.com/stac-utils/stac-fastapi) | Free | Open source | Self-host STAC catalog for cached imagery metadata |
| [pygeoapi](https://pygeoapi.io/) | Free | Open source | OGC API Features from PostGIS layers |

### Imagery, elevation, and environment

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [Copernicus Data Space OData](https://dataspace.copernicus.eu/) | Free | Open data | Direct Sentinel download |
| [NASA Earthdata CMR](https://earthdata.nasa.gov/) | Free | Open data | Cross-catalog satellite search |
| [HLS (Harmonized Landsat Sentinel-2)](https://hls.gsfc.nasa.gov/) | Free | Open data | Consistent 30 m time series |
| [OpenTopography REST API](https://opentopography.org/developers) | Free | Open data | Global lidar and DEM |
| [GMTED2010 / SRTM 1 arc-sec](https://www.usgs.gov/coastal-changes-and-impacts/gmted2010) | Free | Open data | Offline elevation bulk files |
| [OpenLandMap](https://openlandmap.org/) | Free | Open data | Land cover, soil - rural/industrial site context |
| [FIRMS (NASA fire)](https://firms.modaps.eosdis.nasa.gov/) | Free | Open data | Active fire near exploration sites |
| [Global Surface Water (JRC)](https://global-surface-water.appspot.com/) | Free | Open data | Flooded or seasonal access changes |

### Industrial, heritage, and urbex-specific open data

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [OpenInfraMap Overpass tiles/API](https://wiki.openstreetmap.org/wiki/OpenInfraMap) | Free | Open source + Open data | Power plants, substations, pipelines |
| [OpenRailwayMap API](https://wiki.openstreetmap.org/wiki/OpenRailwayMap) | Free | Open source + Open data | Disused rail, yards, depots |
| [OpenSeaMap](https://www.openseamap.org/) | Free | Open source + Open data | Coastal wrecks, piers, harbors |
| [OpenHistoricalMap](https://www.openhistoricalmap.org/) | Free | Open source + Open data | Already implemented; wire to wiki UI |
| [Wikidata SPARQL](https://query.wikidata.org/) | Free | Open data | `P1435` heritage designation, inception dates, coordinates |
| [OpenPlaques](https://openplaques.org/) | Free | Open data | Historical markers at coordinates |
| [GHSL (Global Human Settlement)](https://human-settlement.emergency.copernicus.eu/) | Free | Open data | Built-up change over time |
| [Global ADM (geoBoundaries)](https://www.geoboundaries.org/) | Free | Open data | Admin boundaries worldwide |

### Archives and media (free + openly licensed)

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [Openverse API](https://api.openverse.org/v1/) | Free | Open source + Open data | 700M+ CC-licensed images/audio |
| [Internet Archive advancedsearch](https://archive.org/advancedsearch.php) | Free | Open source + Open data | Full-text + metadata search |
| [DPLA API](https://pro.dp.la/developers/api-codex) | Free | Open data | Already listed; highest-priority archive add |
| [LOC JSON/YAML API](https://loc.gov/api/) | Free | Open data | Already implemented; wire to location wiki |
| [Digital Public Library subject map](https://dp.la/browse-by-topic) | Free | Open data | Thematic browsing for wiki enrichment |
| [Wellcome Collection API](https://wellcomecollection.org/works) | Free | Open data | UK medical/industrial history images |
| [GLAMpipe / Wikimedia Commons API](https://commons.wikimedia.org/wiki/Commons:API) | Free | Open data | Already use Wikimedia; extend structured GLAM uploads |

### Routing, mobility, and trip planning

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [OpenRouteService public API](https://openrouteservice.org/dev/#/api-docs) | Free | Open source + Open data | Daily quota; good RouteXL replacement |
| [GraphHopper Directions API (free tier)](https://www.graphhopper.com/pricing/) | Free / Low cost | Open source + Proprietary hosted | OSS core; hosted free quota |
| [OSRM demo server](http://router.project-osrm.org/) | Free | Open source | Dev/testing only; production = self-host |
| [Valhalla isochrones](https://github.com/valhalla/valhalla) | Free | Open source | "Pins reachable in 2 hours" trip planning |
| [Transitland](https://www.transit.land/) | Free | Open data | Transit access to urban sites |
| [OpenMobilityData (GTFS)](https://transitfeeds.com/) | Free | Open data | Schedule data for trip ETA |

### Weather, astronomy, and photographer tools

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [Open-Meteo](https://open-meteo.com/en/docs) | Free | Open data | No key; historical + forecast |
| [Meteostat JSON API](https://dev.meteostat.net/) | Free | Open data | Decades of station observations |
| [NOAA Climate Data Online](https://www.ncdc.noaa.gov/cdo-web/webservices/v2) | Free | Open data | Historical US weather |
| [SunCalc.js / Python `astral`](https://github.com/sffjunkie/astral) | Free | Open source | Golden hour without external API calls |
| [Clear Dark Sky](https://cleardarksky.com/) | Free | Proprietary data | Astronomical seeing; scrape carefully |
| [Heavens-Above API](https://heavens-above.com/) | Free | Proprietary | Satellite passes for long-exposure planners |

### Search and enrichment without paid SaaS

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [GDELT 2.0 DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | Free | Open data | Location-linked news back to 1979 |
| [Common Crawl CDX](https://index.commoncrawl.org/) | Free | Open data | Find old pages about a factory or site |
| [Wikipedia REST API](https://en.wikipedia.org/api/rest_v1/) | Free | Open data | Summaries, media, geo-search near coords |
| [Wikidata REST / Special:EntityData](https://www.wikidata.org/wiki/Wikidata:Data_access) | Free | Open data | JSON for heritage IDs linked to Wikipedia |
| [SearXNG](https://docs.searxng.org/) | Free | Open source | Self-hosted web search for LoopNet-style fallbacks |

### Safety and government open data (US-focused)

| API / project | Cost | License | Why consider it |
|---|---|---|---|
| [OpenFEMA API](https://www.fema.gov/about/openfema/data-sets) | Free | Open data | Disasters affecting site access |
| [Homeland Infrastructure Foundation (HIFLD)](https://hifld-geoplatform.opendata.arcgis.com/) | Free | Open data | Hospitals, prisons, power plants, dams |
| [EPA Envirofacts API](https://www.epa.gov/enviro/envirofacts-data-service-api) | Free | Open data | Unified environmental facility lookup |
| [USGS National Map WMS/WCS](https://apps.nationalmap.gov/services/) | Free | Open data | Topo, hydro, structures |
| [NOAA Nautical Charts (ENC)](https://nauticalcharts.noaa.gov/) | Free | Open data | Coastal ruin access and tides context |

---

## High-Impact Recommendations (Prioritized)

Best ROI relative to existing architecture, with cost bias toward **Free** options first.

1. **Wire up existing free gateways** - OpenHistoricalMap, Wayback Machine, Google Earth web links, LOC, Digital Commonwealth. Zero new vendors.

2. **Free geocoding headroom** - Self-hosted Nominatim or Photon, or free LocationIQ/OpenRouteService geocode tier before another paid Google alternative.

3. **Open-Meteo + NOAA + Meteostat** - All free/open; weather redundancy and historical shooting conditions.

4. **Copernicus Data Space + Planetary Computer + NAIP** - Free temporal and high-res US aerial beyond Esri Wayback.

5. **DPLA + Openverse + NYPL** - Free/open archive discovery at scale.

6. **EPA ECHO + NRHP + MRDS + OpenInfraMap** - Free structured urbex/heritage/industrial context for location wikis.

7. **OpenRouteService or self-hosted OSRM/Valhalla** - Free routing; replace or supplement RouteXL.

8. **County assessor GIS + TIGER** - Free US parcel fallback vs. LoopNet scraping and paid Regrid.

9. **Wikidata + OpenPlaques** - Free structured enrichment on Wikipedia.

10. **Panoramax + Bing Streetside** - Low-cost or free street-view redundancy.

### Best free + open source quick wins

| Priority | API | Cost | License |
|---|---|---|---|
| 1 | Open-Meteo | Free | Open data |
| 2 | Wikidata Query Service | Free | Open data |
| 3 | Openverse | Free | Open source + Open data |
| 4 | GDELT | Free | Open data |
| 5 | OpenRouteService (public) | Free | Open source + Open data |
| 6 | Copernicus Data Space | Free | Open data |
| 7 | OpenTopography | Free | Open data |
| 8 | OpenInfraMap / OpenRailwayMap | Free | Open source + Open data |
| 9 | Self-hosted Overpass or Nominatim | Free | Open source + Open data |
| 10 | SearXNG (self-hosted search) | Free | Open source |

---

## Integration Pattern

New APIs should plug into existing `Gateway` subclasses with `service_key`, `SERVICE_REGISTRY` rate limits, and provider chains (boundaries, satellite slides, street view) - not one-off controller calls.

For each candidate, ask:

- Does it fill a **geographic gap** (EU, rural US, global)?
- Does it add **redundancy** when Google/Nominatim/Regrid fail?
- Does it provide **urbex-specific signal** (abandonment, heritage, industrial history)?
- Is it **free/open** enough to cache aggressively?
- Can it run as a **background Celery task** with toast completion?

Add new services to `SERVICE_REGISTRY` in `dashboard/services/rate_limiter.py` when integrating.

---

## Sibling Services Found During Property-Records Live Testing (2026-07-19)

Unlike the rest of this document (a general survey), everything below was a *specific, real
endpoint or dataset* that turned up as a sibling item while live-testing the county property-
records discovery pipeline (`services/apis/property_records/`) against ~70 real counties'
ArcGIS Online item-search and portal results. Recorded here rather than acted on, per the plan's
"note candidates, don't build speculatively" approach - these are concrete leads, not the general
aspirational entries above.

### State GIS clearinghouses, encountered as statewide-parcels fallbacks but hosting far more

The discovery pipeline's portal-search fallback (`discover_via_portal_search`) already treats
these as acceptable *statewide parcels* sources for a county with no county-level GIS. Each one's
own AGOL item-search or ArcGIS REST catalog was visibly full of non-parcels layers too, all
reachable through the exact same generic ArcGIS REST client (`arcgis_socrata.ArcGisSocrataGateway`)
already built for this feature - no new gateway class needed to try one, just a new
`PropertyJurisdiction`-style registry row or a one-off query.

| State clearinghouse | Base URL | What else it hosts (observed, not exhaustive) |
|---|---|---|
| Minnesota MnGeo | `enterprise.gisdata.mn.gov` | Statewide parcels (`plan_parcels_open`) plus MnGeo's much broader environmental/infrastructure catalog |
| Colorado | `gis.colorado.gov` | Statewide `Address_and_Parcel/Colorado_Public_Parcels`; likely wildfire/hazard layers under the same portal (not itself explored) |
| Idaho Dept. of Water Resources | `gis.idwr.idaho.gov` | Statewide `Reference/Parcels` layer hosted by the *water* agency - water-rights/well data is presumably adjacent on the same server |
| Missouri Spatial Data Information Service (MSDIS) | `data-msdis.opendata.arcgis.com` | Primary MO geospatial layer clearinghouse; surfaced in Boone County's search results but not explored beyond that |
| Florida | `services9.arcgis.com/.../Florida_Statewide_Cadastral` | Statewide cadastral; likely paired with other FL statewide environmental layers under the same publisher |
| Tennessee | `services1.arcgis.com/.../Tennessee_Property_Boundaries_Public_Use` | Statewide property boundaries; same publisher pattern as above |

### Environmental/hazard layers - directly on-theme for an urbex app

Several turned up as *false positives* to reject during discovery (see `docs/NOTES.md`'s
"Property-records discovery heuristics" section for the specific incidents), but the underlying
datasets are genuinely relevant to this app's actual audience:

- **Groundwater contamination / consent-decree tracking** - Kent County, MI's "Parcel Status from
  February 2020 Consent Decree" layer (`services1.arcgis.com/FNjlrOFR0aGJ71Tg/.../FeatureServer/0`).
  A real litigation-driven environmental tracker over specific parcels - close cousin to the
  already-implemented `epa_echo` plugin, but county/city-published rather than federal.
- **Mine waste tracking** - a West Virginia layer (`Mine_Waste_WFL1`, owner `Davis_lnkinder`)
  surfaced incidentally in a Boone County, MO portal search. Suggests per-state mine-hazard
  layers exist beyond the federal USGS MRDS/OSMRE AML entries already listed above.
- **Flood zones** - `FLD_ZONE` turned up as a raw field/sub-layer name inside more than one
  county's GIS service (alongside Parcels, County_Boundary, etc.) - already listed generically
  as FEMA NFHL above, but confirms individual counties often republish their own flood-zone
  layer on the same ArcGIS server as their parcels, which would be a cheap "already querying
  this host" addition per-county rather than a new national integration.

### Wildlife/conservation corridor data

- Arizona Game & Fish Dept's **"Pima County Wildlife Linkages: Stakeholder Input"** Web Map
  (owner `DPokrajac_AZGFD`) - stakeholder-workshop wildlife-corridor data, state agency-published.
  Not in the existing candidate list above; worth a line item if wildlife-corridor context near a
  pin is ever wanted (adjacent to the existing iNaturalist/GBIF-style entries).

### Trail and outdoor POI layers

- **"Pima County Trailheads"** (`trailheads_east_pima`, owner `AGONRPRpblsh`) - county-published
  trailhead points, same ArcGIS Online pattern as parcels.
- **University tree-survey data** - "Pima County Tree Survey Final", an ASU capstone project
  (owner `fshenk_asu`) hosted publicly on AGOL. Illustrates that academic/capstone GIS projects
  routinely publish real municipal survey data on ArcGIS Online under student accounts - a
  low-reliability but occasionally rich source worth being aware of, not necessarily worth
  integrating (no stable publisher, could disappear without notice).

### Standalone address-point layers

Several county GIS services carried a **separate `AddressPoints` sub-layer** alongside their
Parcels layer (e.g. the Nicholas County, WV service structure encountered while chasing a
discovery bug: `AddressPoints`, `ParcelHooks`, `Roads`, `County_Boundary`, `Surrounding_Counties`,
`TAX_DISTRICTS` all as sibling layers under one service). A free, often more-authoritative
alternative/supplement to the Census geocoder already in use (`services/apis/locations/census_geocoder.py`)
for any county whose Tier 1 parcels endpoint is already configured - same host, zero new
discovery/registry work, just a second query against an already-known server.

---

## Related Code

| Area | Path |
|---|---|
| Gateway base class | `dashboard/services/gateway.py` |
| Satellite / street-view / boundary abstractions | `dashboard/services/apis/locations/base.py` |
| Boundary provider chain | `dashboard/services/locations/boundaries.py` |
| Rate limit registry | `dashboard/services/rate_limiter.py` |
| Pin UI integrations | `dashboard/controllers/pin.py` |
| Search fallback | `dashboard/services/search.py` |
