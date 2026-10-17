![Urban Lens Logo](/src/urbanlens/dashboard/frontend/static/dashboard/images/logo_color-480w.webp)




### Map, log, and share the places worth exploring.

---



## About

UrbanLens is a web mapping platform for photographers and urban explorers. It gives you a personal, private map for pinning locations, keeping notes and photos, planning trips with friends, and pulling in outside context (weather, Wikipedia, historical registries, and more) about the places you want to visit, while staying deliberately low-key about responsible, respectful exploration.

## Feature Highlights

- **Interactive mapping** — layered map with search and filter. Easily create markup maps to save as screenshots, and edit them later when things change.
- **Private pins** — private notes, ratings, aliases, and custom fields. Write a whole article about a spot if you want to.
- **Lists & saved filters** — pin collections, reusable filters, and smart lists that resync automatically
- **Community wikis** — opt-in, community-editable pages per location with voting and edit history; visible only to users who have already discovered the location
- **Location intelligence** — background research from dozens of sources, to help make informed decisions about each location.
- **Trip planning** — plan trips with friends, vote on which places to go, and sync with Google Calendar
- **Photos & Memories** — add photos, and optionally pin places you forgot based on their metadata.
- **Safety check-ins** — "I didn't come home" alerts that escalate to emergency contacts, with live chat. Contacts never see any of your plans unless you don't check-in on time, so you can keep your activities private unless you actually need help.
- **End-to-end encrypted messaging** — reactions, disappearing messages, search, and more. End-to-end encrypted, so your messages are only visible from your device.
- **Social layer** — friendships, granular visibility settings, and comments with mentions and reactions
- **Smart import** — auto-tagging, and update your pin data from external urls
- **Import/export** — Google Takeout, GPX, KML/KMZ, OSM XML, Shapefile, WKT/WKB; full-account export plus scheduled backups
- **Responsible-exploration ethos** — discovery-gated wikis and discretion by design, not public broadcasting


## Tech Stack


| Layer           | Technology                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| Backend         | Django + Django REST Framework, Channels (WebSockets), Celery                                                |
| Database        | PostgreSQL with PostGIS (geospatial queries)                                                                 |
| Frontend        | HTMX-first interactivity, Leaflet for maps, TypeScript/TSX where JS is unavoidable, SCSS, bundled with Bun   |
| Geospatial      | GeoDjango, GeoPandas, Shapely, FastKML, geopy                                                                |
| Auth            | Django auth plus Google and Discord OAuth, passkeys (WebAuthn), and TOTP 2FA                                 |
| External data   | Google Maps/Places, OpenWeatherMap, USGS, Wikipedia, Nominatim, Open-Meteo, and more                         |
| Quality tooling | Ruff, MyPy, pytest-django, Hypothesis, pre-commit                                                            |




## Quick start

Docker with the Compose v2 plugin is all the stack needs on the host. Python 3 is useful for generating secrets.

```bash
git clone https://github.com/UrbanLens/UrbanLens.git
cd UrbanLens
cp .env-sample .env
# Replace the secrets and set UL_SITE_URL and UL_ALLOWED_HOSTS in .env
docker compose up -d --build
```

The sample `.env` is set up for production and refuses to start until its secrets are replaced. [docs/self-hosting/install.md](docs/self-hosting/install.md) covers every required value, TLS, the first admin and email. To run on one machine without TLS, see [section 6 of that page](docs/self-hosting/install.md#6-local-trial-over-plain-http).

## Self-hosting

- [Self-hosting guide](docs/self-hosting/README.md): install, configuration, upgrading, backups and operations.
- [Plugins](docs/plugins/README.md): extending a deployment with plugins.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.

## Security

See [SECURITY.md](SECURITY.md) for supported versions and how to report a vulnerability.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the license terms for contributions, the checks CI runs, and how to open a pull request.

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

UrbanLens is licensed under the GNU Affero General Public License, version 3 only (AGPL-3.0-only). The full text is in [LICENSE.md](LICENSE.md).
