## Project Overview

UrbanLens is a Django mapping application for photographers and urban explorers to organize and
share urbex locations responsibly.

**This project is in beta.** Anything inconsistent or suboptimal is a bug - not a convention to
follow or replicate. When something looks wrong, it probably is.

## Tech Stack

Django 6+, Channels (WebSockets), Celery, PostgreSQL + PostGIS; django-gis, GeoPandas, Shapely, FastKML, geopy, HTMX, TypeScript/TSX, SCSS, bun, Django auth, OAuth2 (Google, Discord), passkeys (WebAuthn), TOTP 2FA

## Documentation

**Nearly everything in `docs/` was written by a Claude agent, not by Jess.** It is
evidence, not authority. Re-measure before relying on a figure, and rewrite the doc when you find
it wrong.

Work to do is a GitHub issue; a decision is an ADR in `docs/adr/`. `docs/INDEX.md` indexes
the rest (`PL` plan, `I` idea, `X` experiment, `R` reference, `N` note), one greppable line per
record: `grep -E '^\| N7 ' docs/INDEX.md`. A former `P#`/`T#` maps to its issue in
`docs/PROBLEMS.md`; former `D#` is the ADR with the same number; resolved problems live in `docs/archive/`.

- `docs/FEATURES.md` - what already exists. Reuse infrastructure rather than rebuilding it.
- `docs/MEDIA_PIPELINE.md` - before touching anything that parses user-supplied bytes.
- `docs/DATA_ENCRYPTION.md` - before touching an encrypted field or rotating a key.

- **Issue tracker:** GitHub Issues on UrbanLens/UrbanLens via `gh`. See `docs/agents/issue-tracker.md`.
- **Triage labels:** the defaults (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.
- **Domain docs:** single-context; the glossary is `docs/CONTEXT.md`, decisions are `docs/adr/`. See `docs/agents/domain.md`.

## Layout

- `src/urbanlens/dashboard/` - the app: `controllers/`, `models/` (a package per entity),
  `services/`, `forms/`, `templates/`, `frontend/` (SCSS + TS), `plugins/`, `migrations/`, `tests/`
- `src/urbanlens/UrbanLens/settings/` - Django config in `base.py`, env-driven app config in
  `app.py` (Pydantic). New env vars go in `app.py`.
- `src/urbanlens/core/` - test runner, base test case, shared infrastructure
- `bin/` (scripts), `tests/contract/` (schemathesis), `tests/integration/` (Playwright), `docs/`
- Repo root: `docker-compose.yml`, `pyproject.toml`, `package.json`

## Linting & Type Checking

Always run ruff with `--fix`. 

**MyPy** finds bugs; it does not exist to be silenced. Fixing a warning can mean correcting code or
types at the origin of the call rather than the point of failure. Never use `cast` or similar. Fix
bad assumptions, implement generics. If you are unsure, leave a TODO rather than silence it.

**pre-commit** Whole-tree invariant checks, CodeQL and the TypeScript suite are manual-only. Run them before a PR: 
`bun run check`, `bun run typecheck`, `bun run test:ts`, `bun run codeql:gate`.

Common commands belong in `pyproject.toml` scripts, `package.json`, and/or VSCode tasks.

## Code Quality

Prefer OOP, inheritance, and generics for abstraction and extensibility.

- Type hints throughout; MyPy with Django stubs
- Modern Python (3.12+); prefer actively maintained libraries over dated equivalents
- Google docstrings, complete enough to generate reference documentation from 

Comments should be concise, and only included when not obvious. Assume someone competent will work
on this after you; unnecessary explanation is a burden. If an explanation is necessary, say why the
approach is used now, not its history. Do not state design decisions authoritatively - it
discourages reassessing them later.

## Testing

Use pytest and Model Bakery. Test everything substantive.

Run targeted regression tests for most work, with `--reuse-db` for speed (at the cost of potential
collisions), and set a unique `UL_TEST_DB_NAME` so parallel sessions don't collide. Run the full
suite without `--reuse-db` once before merging a PR - it takes a while, so do other work meanwhile.

When a bug is reported, reproduce it with a failing test first, then fix it, per TDD.

## Git Workflow

Commit every batch of changes without waiting to be asked - a "batch" is one logically complete
unit of work. Push once the work you were asked to do is complete, without being asked.

For complex tasks, pause and review your work with fresh eyes before finishing (in Claude Code, set a
2-minute wakeup first).
