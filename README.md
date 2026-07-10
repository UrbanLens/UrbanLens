![Urban Lens Logo](/src/urbanlens/dashboard/frontend/static/dashboard/images/logo_color-480w.png)

[![CI](https://github.com/UrbanLens/UrbanLens/actions/workflows/ci.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/ci.yml) [![Security](https://github.com/UrbanLens/UrbanLens/actions/workflows/security.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/security.yml) [![Release Please](https://github.com/UrbanLens/UrbanLens/actions/workflows/release-please.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/release-please.yml)


### Map, log, and share the places worth exploring.

[CI](https://github.com/UrbanLens/UrbanLens/actions/workflows/ci.yml)
[Security](https://github.com/UrbanLens/UrbanLens/actions/workflows/security.yml)
[Release Please](https://github.com/UrbanLens/UrbanLens/actions/workflows/release-please.yml)



---



## About

UrbanLens is a web mapping platform for photographers and urban explorers. It gives you a personal, private map for pinning locations, keeping notes and photos, planning trips with friends, and pulling in outside context (weather, Wikipedia, historical registries, and more) about the places you want to visit, while staying deliberately low-key about responsible, respectful exploration.

## Feature Highlights

- **Interactive mapping** — pin locations, draw campus/boundary areas, and browse layered map views built on Leaflet + HTMX
- **Personal notes & status** — track visit status, priority, and private notes per pin, independent of the shared location data
- **Trip planning** — organize multi-stop trips with friends, RSVPs, and scheduling
- **Social layer** — friendships, reviews, and shared trips
- **Location intelligence** — on-demand enrichment from external sources (weather, Wikipedia, Wikimedia Commons, National Park Service, and others), cached to keep API usage in check
- **Photo galleries** — attach and browse images per pin and per location
- **Responsible-exploration ethos** — the product is built around discretion and respect for the places it helps you track, not public broadcasting



## Tech Stack


| Layer           | Technology                                                                                                   |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| Backend         | Django + Django REST Framework, Channels (WebSockets), Celery                                                |
| Database        | PostgreSQL with PostGIS (geospatial queries)                                                                 |
| Frontend        | HTMX-first interactivity, Mapbox GL for maps, TypeScript/TSX where JS is unavoidable, SCSS, bundled with Bun |
| Geospatial      | GeoDjango, GeoPandas, Shapely, FastKML, geopy                                                                |
| Auth            | Django auth plus Google and Discord OAuth                                                                    |
| External data   | Google Maps/Places/Search, OpenWeatherMap, Smithsonian, National Park Service, OpenAI, and more              |
| Quality tooling | Ruff, MyPy, pytest-django, Hypothesis, pre-commit                                                            |




## Getting Started

The project is designed to run entirely through Docker, so you don't need Python, Node, or PostGIS installed locally.

1. Clone the repository.
2. Copy the environment template and fill in the values you need:
  ```bash
   cp .env-sample .env
  ```
   Every variable in `.env-sample` is documented inline, including where to get each external API key. You can leave integrations you don't need blank — the app degrades gracefully without them.
3. Build and start the stack:
  ```bash
   docker-compose up --build
  ```
4. Visit the app at `http://localhost:21800` (nginx serves the app; the port is configurable via `UL_APP_PORT`).

Docker Compose brings up the Django app, Celery worker/beat, PostgreSQL/PostGIS, Valkey (Redis-compatible cache/broker), and nginx together.

## CI/CD and Releases

UrbanLens uses GitHub Actions for automated quality gates, security checks, release preparation, and artifact publishing:

- Pull requests and pushes to `main` run Python linting, formatting checks, type checks, Django checks, tests with coverage, frontend asset builds, and a Docker image build.
- Security automation runs CodeQL, dependency review for pull requests, and scheduled weekly scans.
- Dependabot opens weekly updates for GitHub Actions, Python, npm, and Docker dependencies.
- Releases are managed with [Release Please](https://github.com/googleapis/release-please). Use Conventional Commit messages such as `feat: add route export` or `fix: handle empty weather data` — merges to `main` update a release PR, and merging that PR creates the version tag, changelog, and GitHub release.
- Published GitHub releases build Python distributions and a provenance-attested container image in GitHub Container Registry (`ghcr.io/UrbanLens/UrbanLens`), tagged with the semantic version and `latest` for stable releases.



## Security

See [SECURITY.md](SECURITY.md) for supported versions and how to report a vulnerability.

## Contributing

Contributions are welcome. Please:

- Follow the conventions and architectural patterns established in the codebase.
- Use Conventional Commit messages (they drive automated releases — see above).
- Run Ruff before committing, and include tests for new functionality.
- Open a pull request; CI will run the full quality gate automatically.

