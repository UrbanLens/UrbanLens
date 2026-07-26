# dashboard/models/ — Model-Specific Guidance

Applies to `src/urbanlens/dashboard/models/`.

## Location vs Pin - core distinction

These three models are often confused. Keep their responsibilities strictly separate:

**`Location`** - shared, globally recognised data for multiple users about a physical place.
- Canonical name, description, address components, coordinates, Google Maps CID
- Not user-specific - many users may have pins referencing the same Location
- The authoritative source for address, place metadata, and geo coordinates
- Links to `Pin` and `Wiki` to provide geolocation details to them.
- Users have no direct access to this (for instance: through the api), otherwise we would leak the existance of locations to users who don't yet have them pinned.

**`Pin`** - a specific user's personal record for a location.
- `location` FK pointing at the shared Location
- User-specific fields: custom name override, personal notes (`description`), icon, priority, last-visited date, status, and marker coordinates
- Address and place metadata are accessed via read-only proxy properties that delegate to `self.location`; never store address data directly on Pin. If the pin moves, assign it to a new location rather than mutating the location it was assigned to.

**`Wiki`** - Community wiki for a location that many users can see and edit. The only users who can see a wiki are users who have a pin within the bounding box of the wiki's location. This is by design; users must discover the location before they can see the wiki.

## Models & ORM

All models inherit from `urbanlens.dashboard.models.abstract.Model`, which provides `created`/`updated` fields and the custom manager.

Each entity under `models/` has:
- `model.py`
- `queryset.py`
- `serializer.py`
- `viewset.py`
- `filterset.py`

**QuerySet/Manager pattern**: Custom QuerySets on `objects` handle filtering, annotation, and geo queries (PostGIS `__distance`, `__contains`, etc.). Always use `select_related`/`prefetch_related` appropriately.

## Common Patterns

1. Use `TYPE_CHECKING` guard for imports that would cause circular references in models
2. Always `prefetch_related` for M2M, `select_related` for FK to avoid N+1 queries
3. PostGIS geo queries use django-gis operators (`__distance_lte`, `__contains`, etc.)

**Signals & saves**
- Always pass `dispatch_uid` when connecting signals.
- The linter has been observed stripping early-return guards from signal handlers; write handlers so the guard is redundant rather than load-bearing.
