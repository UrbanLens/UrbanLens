# CLAUDE.md

This is a living document that may contain inaccuracies. When in doubt, verify against the codebase.

## Project Overview

UrbanLens is a Django mapping application for photographers and urban explorers to organize and share urbex locations responsibly. The stack includes Django 6+, PostGIS, HTMX, and TypeScript/SCSS.

**This project is in beta.** Anything inconsistent or suboptimal are bugs - not conventions to follow or replicate. When something looks wrong, it probably is.

**Docs** - `docs/FEATURES.md` - an inventory of features. Infrastructure should be reused whenever possible (e.g. a generic height-based client pagination system, a shared visit dialog, shared map toolbar/layers components). `docs/NOTES.md` - explains some non-obvious behavior; `docs/designs/plugins.md` - documents the plugin contribution API.

`docs/TOOLING.md` - the diagnostic and CI tooling: how to run tests fast, mutation
testing, the two "where to look" reports, the three structural CI checks, and the
shared test helpers. Each entry records the defect that motivated it, so its value
does not have to be re-derived.

`docs/INTEGRATION_TESTS.md` - the on-demand Playwright suite in `tests/integration/`,
run by hand against staging. It answers what the pytest suite structurally cannot:
whether the deployed pieces work together. Read it before adding a test there -
particularly the rules about waiting for HTMX and never signing out on the shared
session.

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

Docstrings: Use Google-style docstrings. These will be consumed by Sphinx for ReadTheDocs-style documentation - completeness matters.

- Type hints throughout; MyPy with Django stubs
- Modern Python (3.12+)
- Choose actively maintained, modern libraries over dated equivalents

**Comments**:
Comments should be concise, and only included when not obvious. Assume that someone competent and familiar with the tech, will be working on code after you; unnecessary explanation is a burden.

If an explanation is necessary, only explain why this approach is used now, not its history. Do not authoritatively state we've made design decisions, because it discourages reassessing our implementation in the future.

## Testing

Use Model Bakery for fixtures. Use pytest.

Test everything substantive. Do not create trivial unit tests, such as to test a logging message; this causes minor changes to result in tests failures. Mock and patch, especially when testing anything that contacts an external service. Use hypothesis property-based tests whenever possible. Use SimpleTestCase when DB access isn't needed.

Set `UL_TEST_DB_NAME` to a unique value when running pytest, so parallel agent sessions don't collide.

When a user reports a bug, reproduce it with a failing unit test (TDD), then fix - the test guards against regression.

## Common Patterns

- Anything non-instant must display a loading/progress indicator
- Results and errors must surface as toast notifications
- Prefer HTMX partial page updates

- Settings are split: Django config in `settings/base.py`, app env-driven config in `settings/app.py` (Pydantic)
- Any new pin/location share path must call `resolve_origin_share` + `record_share_exposure` to
  keep the `LocationExposure` provenance chain intact.
- `EncryptedTextField` writes under the active key and reads under any retired key still listed in `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`; rotate keys only via the procedure in `docs/DATA_ENCRYPTION.md` (which also tracks what is/isn't encrypted, and why)