# dashboard/ — App-Specific Guidance

Applies to `src/urbanlens/dashboard/`. 

## Views & Controllers

REST API endpoints use DRF ViewSets under `/rest/`. Custom actions use `@action(detail=True, methods=[...])`.

Template/HTMX views use `TemplateView` or `ViewMixin` and return rendered HTML. Key controllers:
- `maps.MapController` - Map display, pin add/edit, search
- `pin.PinController` - Pin detail, image/weather/search integrations
- `userprofile.ViewProfileView/EditProfileView` - Profile CRUD

## URL Routing

```
dashboard/
├-- rest/         → DRF router (ViewSets)
├-- map/          → MapController (view, add, edit, search)
│   └-- pin/<id>/ → PinController (detail)
├-- profile/      → ProfileController
└-- friendship/   → FriendController
```

User-facing objects are slug-addressed: trips are `/trips/<slug>/` (not uuid), etc.

## Frontend & HTMX Philosophy

**HTMX is preferred for interactivity.** Features should use HTMX (hx-get, hx-post, hx-swap, etc.) to request server-rendered HTML fragments, minimizing JavaScript. Use Typescript only when HTMX cannot accomplish the interaction. Every existing JS interaction is a candidate for HTMX refactoring.

- SCSS source: `src/urbanlens/dashboard/frontend/sass/style.scss` → compile with `bun run sass`
- Templates in `src/urbanlens/dashboard/templates/dashboard/`

## API Integrations

The project connects to many external APIs via service classes in `dashboard/services/`. Each service wraps one API. 

External integrations are wired into the app through the plugin system: the API client stays a `Gateway` subclass in `dashboard/services/apis/`, and a small `UrbanLensPlugin` subclass (bundled ones live in `dashboard/plugins/builtin/`) declares its rate-limit defaults and contributions (pin-detail panels, imagery providers, hook callbacks). New integrations should be added as plugins; services not yet converted still register their defaults in `rate_limiter.SERVICE_REGISTRY`.

API usage and cost tracking is **automatic**: the `Gateway` base wraps every request in a
rate-limited session that writes an `ApiCallLog` row (with a `cost_estimate`) per call - no
per-integration code needed. Only code that bypasses `self.session` (a bare `requests.*` call,
an SDK client) must record itself via `rate_limiter.log_api_call`, as the AI services do.

## Gotchas

- Any new pin/location share path must call `resolve_origin_share` + `record_share_exposure` to
  keep the `LocationExposure` provenance chain intact.
- `EncryptedTextField` writes under the active key (`UL_FIELD_ENCRYPTION_KEY`, else Django's
  `SECRET_KEY`) and reads under any key in `UL_FIELD_ENCRYPTION_KEY_FALLBACKS`. Changing a key
  is safe *only* via the rotation procedure in `docs/DATA_ENCRYPTION.md` (add new key → run
  `manage.py rotate_field_encryption` → drop old key); swapping it in one step orphans every
  row. Content fields set `fail_soft=True` and degrade to their default rather than raising;
  credential fields raise so their callers can drop the row and prompt a reconnect.
