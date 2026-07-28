# CLAUDE.md

This is a living document that may contain inaccuracies. When in doubt, verify against the actual codebase and project documentation.

## Project Overview

UrbanLens is a Django mapping application for photographers and urban explorers to organize and share urbex locations responsibly. The stack includes Django 6+, PostGIS for spatial data, HTMX for interactivity, and a TypeScript/SCSS frontend.

**This project is under active development.** Irregularities, inconsistencies, or suboptimal patterns in existing code are bugs to be fixed - not conventions to follow or replicate. When something looks wrong, it probably is.

**Features listed in `docs/FEATURES.md`** - a maintained inventory of everything already built (mapping/pins, lists/smart lists/saved filters, wikis, trips, safety check-ins, E2EE direct messages + group chats, notifications matrix, photos/Memories, plugin system, AI integration, passkeys/TOTP, undo framework, site admin, and more). Many "new feature" requests are already implemented or have reusable infrastructure (e.g. a generic height-based client pagination system, a shared visit dialog, shared map toolbar/layers components, the undo framework). `docs/NOTES.md` explains non-obvious behavior behind features; `docs/plugins.md` documents the plugin contribution API.

## Development Environment

`CLAUDE.local.md` at the repo root contains environment-specific info.

## Quick Start

### Linting & Type Checking

Always run ruff with `--fix`, so you don't waste time looking at issues ruff can solve. `migrations/`, `settings/`, `tests/`, and `__init__.py` are excluded from ruff by the config in `pyproject.toml`.

**MyPy**
When examining mypy output, never use cast or similar solutions. Remember that the purpose of mypy is to find real errors and improve code quality, not to silence warnings. This will sometimes require going back to the origin of the call and adjusting types, rather than trying to paper over it at the point of failure. If the code at the origin is making a false assumption, fix the bug. Doing things like implementing generics is needed to address some types of mypy warnings. If you're unsure, mark it as a TODO instead of doing things to silence the warning.

**pre-commit**: runs twice (first pass silent) so formatting fixes are applied before real failures are output.

> Common development commands should be consolidated into `pyproject.toml` scripts, `package.json`, and/or VSCode tasks - add new ones there rather than leaving them undocumented.

## Project Structure

```
src/
|-- urbanlens/
|   ├-- manage.py
|   ├-- UrbanLens/
|   │   ├-- settings/                # Django settings (local.py, app.py, __init__.py)
|   │   ├-- urls.py                  # Root URL configuration
|   │   ├-- wsgi.py / asgi.py        # WSGI/ASGI applications
|   │   └-- environments/            # Environment-specific configuration
|   ├-- dashboard/                   # Main app (maps, pins, profiles, reviews, trips)
|   │   ├-- controllers/             # ViewSet/View classes
|   │   ├-- models/                  # Data models (organized by entity)
|   │   │   ├-- abstract/            # Base classes (Model, QuerySet, Manager, ViewSet, Serializer)
|   │   │   ├-- pin/                 # Pin model, serializers, querysets, viewsets
|   │   │   ├-- profile/             # User profile model
|   │   │   ├-- reviews/             # Review model
|   │   │   ├-- friendship/          # Friendship relationships
|   │   │   ├-- images/              # Image attachments
|   │   │   ├-- categories/          # Pin categories
|   │   │   ├-- location/            # Location data
|   │   │   ├-- labels/              # Labels (tags, categories, statuses, people labels)
|   │   │   ├-- trips/               # Trip planning
|   │   │   └-- cache/               # Geocoding cache
|   │   ├-- services/                # Business logic (AI, search, weather, geocoding, APIs)
|   │   ├-- frontend/
|   │   │   ├-- sass/                # SCSS source
|   │   │   ├-- ts/                  # TypeScript/React source
|   │   │   └-- static/              # Compiled output
|   │   ├-- templates/dashboard/     # Django templates
|   │   ├-- forms/                   # Django forms
|   │   ├-- migrations/              # Database migrations
|   │   └-- urls.py                  # Dashboard URL routes
|   ├-- core/
|   │   ├-- tests/                   # Custom test runner and base test case
|   │   └-- controllers/             # DB backups, init scripts
|-- bin/                             # Startup and utility scripts
docs/
|-- prompts/                         # Historical work notes from LLM agents
|-- reports/                         # Data collected for specific tasks
```

### Key Configuration Files

- **`src/urbanlens/UrbanLens/settings/base.py`**: Django settings (DEBUG, installed apps, middleware, database)
- **`src/urbanlens/UrbanLens/settings/app.py`**: Pydantic-based settings (env vars, paths, feature flags)
- **`docker-compose.yml`**: Service orchestration (app, nginx, PostGIS)
- **`pyproject.toml`**: Dependencies, Ruff/MyPy/Pylint/Yapf config, dev scripts
- **`package.json`**: Node/Bun scripts (sass, build, migrations)

## Tech Stack

- **Backend**: Django >= 6, Django REST Framework, Channels (WebSockets)
- **Database**: PostgreSQL with PostGIS for geospatial queries
- **Frontend**: SCSS, TypeScript/TSX, Bun bundler, **HTMX** for interactivity
- **Authentication**: Django auth + social-auth (Google OAuth2, Discord OAuth2), passkeys (WebAuthn) + TOTP 2FA
- **Async tasks**: Celery (note the dedicated `panel_fetch` queue for pin-detail panel fetches; CPU-heavy panels opt out via `PanelSource.queue`)
- **Geospatial**: django-gis, GeoPandas, Shapely, FastKML, geopy
- **APIs integrated**: OpenAI, Google Places/Maps/Search, Smithsonian, OpenWeatherMap, NPS, and others
- **Other**: Ruff, MyPy, pytest-django, Model Bakery

## Architecture Patterns

OOP is the standard approach throughout this codebase. Prefer inheritance (with Python generics where applicable) for abstraction and extensibility - this avoids code duplication across similar models, views, serializers, and services. Base classes live in `dashboard/models/abstract/` and should be used as the foundation for new code.

## Code Quality Standards

**Docstrings**: All classes and methods must have Google-style docstrings (Args, Returns, Raises sections). These will be consumed by Sphinx for ReadTheDocs-style documentation - completeness matters.

**Preferred patterns**:
- Ruff enforces Google style, 4-space indentation, max line length 250
- Type hints throughout; MyPy with Django stubs
- Modern Python (3.12+) and modern package versions over legacy alternatives
- Choose actively maintained, modern libraries over dated equivalents

## Testing Infrastructure

Custom runner in `urbanlens.core.tests.runner.TestRunner` (extends DiscoverRunner):
- Uses Model Bakery for fixture generation
- Suppresses logs on passing tests, surfaces them on failure

Do not create unit tests for trivial code, such as __init__.py, or to test that a logging message precisely matches a string, especially when it will cause extremely minor changes to result in tests failing. Make sure to mock and patch appropriately, especially when testing anything that contacts an external service. Add hypothesis property-based unit tests whenever possible. Use SimpleTestCase when DB access isn't needed.

**Test-running gotchas**:
- Use pytest, not `manage.py test`
- **Set `UL_TEST_DB_NAME`** to a unique value when running pytest, so parallel agent
  sessions don't collide.

- When a user reports a bug you plan to fix, first reproduce it with a failing unit test (TDD),
  then fix - the test guards against regression.

## Roadmap / Known TODOs

These are planned features - treat any missing implementation as a gap to fill, not a deliberate omission:

- **AI support**: Add AI-assisted suggestions and customization throughout the application (a pluggable AI gateway with import/tagging/link-extraction features exists - extend it)
- **Celery / async tasks**: all non-instant UI operations must show a progress indicator and use toast notifications on completion or failure
- **Hypothesis unit tests**: Add property-based tests wherever possible.

More specific details on the roadmap and TODO items are in ROADMAP.md

## UI & UX Standards

- Any operation that is not near-instant must display a loading/progress indicator
- Results and errors must surface as toast notifications (toastr is already integrated)
- Prefer HTMX-driven partial page updates over full reloads

## Common Patterns

1. Settings are split: Django config in `settings/base.py`, app-level env-driven config in `settings/app.py` (Pydantic)

**Other**
- Any new pin/location share path must call `resolve_origin_share` + `record_share_exposure` to
  keep the `LocationExposure` provenance chain intact.
- `EncryptedTextField` derives its key from Django `SECRET_KEY` - changing it corrupts all
  encrypted data (Immich tokens, etc.).

## Additional Notes
The application is in beta; if you notice quirks, bugs, or poorly implemented code, it should not be assumed this is by design. Investigate and fix problems that you identify. If you can't fix or investigate a problem in the current scope of your work, immediately note the problem in a file docs/PROBLEMS.md to investigate and address later.

## Testing

When the user points out incorrect behavior and bugs, and you plan to replicate the behavior, you should do that by creating a unit tests via TDD. That unit test will then be useful after fixing the problem to ensure the behavior does not return.