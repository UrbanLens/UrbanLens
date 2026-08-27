# CLAUDE.md

This is a living document that may contain inaccuracies. When in doubt, verify against the codebase.

## Project Overview

UrbanLens is a Django mapping application for photographers and urban explorers to organize and share urbex locations responsibly. The stack includes Django 6+, PostGIS, HTMX, and TypeScript/SCSS.

**This project is in beta.** Anything inconsistent or suboptimal are bugs - not conventions to follow or replicate. When something looks wrong, it probably is.

**Docs** - 

Files in `docs/`

`FEATURES.md` - an inventory of features. Infrastructure should be reused whenever possible (e.g. a generic height-based client pagination system, a shared visit dialog, shared map toolbar/layers components). 

`NOTES.md` - explains some non-obvious behavior; 

`designs/plugins.md` - plugin contribution API.

`TOOLING.md` - the diagnostic and CI tooling

`TEST_COVERAGE_GAPS.md` - every defect the integration/contract suites found
that the pytest suite did not

`CONTRACT_TESTS.md` - the schemathesis suite in `tests/contract/`.

`INTEGRATION_TESTS.md` - the on-demand Playwright suite in `tests/integration/`

`LOCATION_DATA_TESTS.md` - the opt-in `location` project inside that suite

`CLAUDE.local.md` at the repo root contains environment-specific info.

## Quick Start

### Linting & Type Checking

Always run ruff with `--fix`.

Note what it does *not* cover: `pyproject.toml`'s `extend-exclude` skips `tests`, `settings`,
`migrations` and `__init__.py`, and those are bare path components, so **every** test directory is
excluded - the whole test suite's source is unlinted. A clean `ruff check src/urbanlens` says
nothing about test code, which is why unused imports survive in e.g. `core/tests/result.py`. Lint
those files by hand or by pointing ruff at them explicitly if it matters.

**MyPy**
The purpose of mypy is to find bugs and improve code quality, not to silence warnings. This sometimes requires going to the origin of the call to adjust types, rather than papering over it at the point of failure. Never use "cast" or similar fixes. Fix bad assumptions and types, implement generics. If you're unsure, mark it as a TODO instead of silencing the warning.

**pre-commit**: runs twice (first silent), so fixes are applied before real failures are output.

Common development commands should be added to `pyproject.toml` scripts, `package.json`, and/or VSCode tasks.

## Git Workflow

Commit every batch of changes without waiting to be asked - a "batch" is one
logically complete unit of work. Push after the work you were asked to do is complete, without waiting to be asked.

## Project Structure

```
src/
|-- urbanlens/
|   ├-- manage.py
|   ├-- UrbanLens/
|   │   ├-- settings/
|   │   ├-- urls.py
|   │   ├-- wsgi.py / asgi.py
|   │   └-- environments/
|   ├-- dashboard/
|   │   ├-- controllers/
|   │   ├-- models/
|   │   │   ├-- abstract/
|   │   │   ├-- pin/
|   │   │   ├-- profile/
|   │   │   ├-- location/
|   │   │   ├-- labels/
|   │   │   ├-- trips/
|   │   │   └-- cache/
|   │   ├-- services/
|   │   ├-- frontend/
|   │   ├-- templates/dashboard/
|   │   ├-- forms/
|   │   ├-- migrations/
|   │   └-- urls.py
|   ├-- core/
|-- bin/
docs/
```


- **`src/urbanlens/UrbanLens/settings/base.py`**: Django base settings
- **`src/urbanlens/UrbanLens/settings/app.py`**: Pydantic settings (env vars, paths)
- **`docker-compose.yml`**
- **`pyproject.toml`**
- **`package.json`**

## Tech Stack
- **Backend**: Django >= 6, Channels (WebSockets)
- **Database**: PostgreSQL with PostGIS
- **Frontend**: SCSS, TypeScript/TSX, Bun bundler, **HTMX** for interactivity
- **Authentication**: Django auth + social-auth (Google OAuth2, Discord OAuth2), passkeys (WebAuthn) + TOTP 2FA
- **Async tasks**: Celery (note `panel_fetch` queue; CPU-heavy panels opt out via `PanelSource.queue`)
- **Geospatial**: django-gis, GeoPandas, Shapely, FastKML, geopy
- **Other**: Ruff, MyPy, pytest-django, Model Bakery

## Code Quality
Prefer OOP, inheritance, and generics for abstraction and extensibility.

Use Google docstrings. These will be consumed by Sphinx for ReadTheDocs-style documentation - completeness matters.

- Type hints throughout; MyPy with Django stubs
- Modern Python (3.12+)
- Choose actively maintained, modern libraries over dated equivalents

Comments should be concise, and only included when not obvious. Assume that someone competent and familiar with the tech, will be working on code after you; unnecessary explanation is a burden.

If an explanation is necessary, only explain why this approach is used now, not its history. Do not authoritatively state we've made design decisions, because it discourages reassessing our implementation in the future.

## Testing

Use pytest and Model Bakery.

Test everything substantive. Do not create trivial unit tests, such as testing a logging message; this causes minor changes to result in tests failures. Mock and patch. Use hypothesis property-based tests whenever possible. Use SimpleTestCase often, and whenever DB access isn't needed.

Run tests with --reuse-db to make them fast, at the cost of potential collissions. Run only targetted regression tests for most work. Run the full suite without --reuse-db one final time before merging a PR. Running many unit tests often takes time, so if you have additional work to do, do it while the test runs.

Set a unique `UL_TEST_DB_NAME`, so parallel agent sessions don't collide.

When a user reports a bug, reproduce it with a failing test (TDD), then fix - the test guards against regression.

## Common Patterns

- Anything non-instant must display a loading/progress indicator
- Results and errors must surface as toast notifications
- Prefer HTMX partial page updates

- Settings are split: Django config in `settings/base.py`, app env-driven config in `settings/app.py` (Pydantic)
- Any new pin/location share path must call `resolve_origin_share` + `record_share_exposure` to
  keep the `LocationExposure` provenance chain intact.
- `EncryptedTextField` writes under the active key and reads under any retired key still listed in `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`; rotate keys only via the procedure in `docs/DATA_ENCRYPTION.md` (which also tracks what is/isn't encrypted, and why)