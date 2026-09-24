# The Hudson River State Hospital specs

`tests/integration/specs/location/` exercises one real place end to end - the
former Hudson River State Hospital campus in Poughkeepsie, NY, which has been
this project's development reference since the beginning. It is the only part of
the test suite that asks whether the *place* pipelines actually work: parcel
resolution, building discovery, child pins, the community wiki, external media,
property records and the floorplan footprint.

It is off by default. Turning it on costs real money and real time.

```bash
# The account must be able to make outbound calls, or every spec is a no-op.
# `subscriber` holds property_owners, for the ownership specs; see INTEGRATION_TESTS.md.
# `neighbour` holds the courtyard HRSH pin, so primary and secondary stay strangers for the social specs.
python src/urbanlens/manage.py provision_integration_env --roles primary,secondary,subscriber,neighbour \
    --subscriber-roles subscriber --external-apis --out /tmp/e2e.json

UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json bin/run_integration_tests.sh --url http://localhost:21810 --project location
# or, from tests/integration with UL_E2E_LOCATION_DATA=1 exported: npm run test:location
```

## Why a separate project

Three reasons, and each one is also a reason not to fold these into `api` or `ui`:

- **They spend money.** Every run makes billable calls to REData (and through it
  county GIS and NY SHPO's CRIS), EPA ECHO, Wikipedia and imagery providers.
- **They are slow.** The parcel wait alone is up to ten minutes, because a
  timeout there is meant to mean "it is not coming" rather than "it was slow".
  The project's test timeout is fifteen minutes.
- **They depend on the outside world.** County data changes. A new deed is
  recorded. A provider goes down. That is a legitimate reason for a spec to
  report something, and an illegitimate reason for the main suite to go red.

The project runs with **one worker**, deliberately: the specs share a single pin
on a single property, and the application enforces one root pin per property per
profile, so a second worker would be refused and a third would delete the pin out
from under the others.

## What the numbers here are, and are not

Everything asserted falls into one of three shapes, and the distinction is the
whole design:

1. **Invariants of the application.** "Five coordinates on one parcel resolve to
   one property." "The most recent sale is the first row returned." "A masked
   pinned-user count never renders a bare number." These are what the specs are
   really for.
2. **Bounds, not values.** The parcel's area is asserted to be between 50,000
   and 1,500,000 m² - roughly half to eleven times a live REData measurement of
   the actual parcel (133,964 m²). An earlier version of this bound used the
   widely-reported "~156 acres" figure and set the floor at 200,000 m²; that
   rejected the correct parcel, since REData's real measurement comes in under
   it. It is not checking accuracy. It is catching the two failures
   `services/apis/locations/boundaries/redata.py` documents for this kind of
   site: a too-small building-derived hull selected instead of the parcel, and
   the ~1,040-acre CRIS archaeological sensitivity zone.
3. **Questions, not verdicts.** The expected owner fragment ("Hudson Heritage")
   is the name this suite was given, and public reporting also names EFG-Saber
   Heritage SC, LLC as the entity running the redevelopment. A deed holder and a
   developer are different things, so a mismatch is raised as something for a
   human to settle, with both names in the message - not as an application
   defect.

**No date is hardcoded anywhere.** There is no `last_sale_date` field in the
application at all; "the last sale" is whatever sorts first under
`WikiPropertySale.Meta.ordering` (`["-sale_date", "-created"]`). So the specs
assert the *ordering contract* and that nothing is dated in the future. A deed
recorded tomorrow satisfies both.

## The campus pin, and the courtyard pin

`specs/location/fixtures.ts` builds one fixture per *site* (`SiteConfig`). Two sites stand on the same
tax parcel (3532 North Rd):

| fixture | account | point | private name |
|---|---|---|---|
| `campus` | `primary` | `HRSH_PIN` (41.73328, -73.92812) | "e2e private campus notes" |
| `courtyard` | `secondary` | `COURTYARD_PIN` (41.73266, -73.92736) | "e2e private courtyard notes" |

The courtyard is the point Jess pinned on k3s-staging, where it got a circle, a road for a title and one
building in the CRIS card (P145). It needs a second account because an account holds one root pin per
property; a second root pin on the parcel would be nested or refused. `hrsh-naming.spec.ts` runs every
test at both points: the parcel polygon, one shared wiki, the exact National Register title (D20), the
register and Wikipedia titles as aliases with no building or road names, BLDG 45 among the building child
pins, the Wikipedia link and article, and the CRIS card's campus heading and roster.

Each site is set up once per run:

1. With `UL_E2E_HRSH_FRESH=1`, deletes the account's root pins on the campus and
   their child pins (`DELETE pins/<slug>/?children=delete`). The Location, and with
   it the boundary and the wiki, survives: this retests the pin, not the place.
2. Adopts the account's root pin nearest the site's point, or creates one there
   with the site's private name.
   That name holds no real name, so a wiki titled from it has copied private data.
   `campus.nameIsPrivate` is false for a pin adopted from an older run with a real
   name; run with FRESH to get a private one.
3. Starts enrichment the way a user does: opens `/dashboard/map/pin/<slug>/` signed
   in as the owner, and waits for the page's own `/boundary/` request, which
   schedules the boundary chain. The external API's `panels/boundary/` is never
   called. `campus.visit` and `campus.log` record what the visit saw.
4. Polls `GET pins/<slug>/` (a pure read) for up to ten minutes for a parcel.

The pin, the name at setup, the visit and the verdict are kept in
`reports/run-state/hrsh-campus.json` (the courtyard's under its own key), keyed on the run (`lib/run.ts`), so a worker
restarted after a failure resumes instead of waiting again. Waits that ran out
(`waitForWiki`, `waitForChildPins`) are remembered the same way, so a stalled
pipeline costs one timeout per run, not one per test.

## Reading a failure

The directory is arranged so a broken pipeline produces **one** red, not thirty.
The campus fixture never throws once it has a pin; it carries either the geometry
or a diagnosis. `hrsh-boundary.spec.ts` is the single spec that reports missing
geometry as a failure. Everything else calls `campus.requireBoundary()` and skips
with a pointer to it.

So: **read the boundary failure first.** If it is red, the skips below it are
consequences, not separate problems.

## Metrics

Recorded through `lib/metrics.ts` and compared by `npm run metrics:report` (see
`INTEGRATION_TESTS.md`, "Metrics"):

| Metric | Unit | Recorded by |
| --- | --- | --- |
| `hrsh.boundary.parcel_arrived` | 0/1 | campus setup |
| `hrsh.boundary.seconds_to_parcel` | s, from the pin page visit | campus setup, when it waited |
| `hrsh.boundary.area_sqm` | m² | campus setup |
| `hrsh.pin_page.setup_visit.{ttfb,dom_content_loaded,load}_ms` | ms | the triggering visit |
| `hrsh.pin_page.{ttfb,dom_content_loaded,load}_ms` | ms | `openPrivatePin` |
| `hrsh.child_pins.count`, `hrsh.child_pins.seconds_to_min` | count, s | `waitForChildPins` |
| `hrsh.wiki.available`, `hrsh.wiki.seconds_to_available` | 0/1, s | `waitForCampusWiki` |

## Waiting

The suite's `lib/waiting.ts` exists for this directory. Playwright's `expect.poll`
covers the simple cases; what it does not give is a diagnosis when the wait runs
out, which for a pipeline with a dozen stages is the entire difference between a
useful failure and a useless one. `waitFor` names what it was waiting for, how
many times it looked, and what it last saw.

Four latency classes, and they are not interchangeable:

| Work | Where it runs | Realistic wait |
| --- | --- | --- |
| Pin creation, place resolution from known geometry | The request | Synchronous |
| Alias mirroring, EPA link writes | `transaction.on_commit`, in process | Seconds |
| Panels and media | Celery, **`panel_fetch` queue** | Up to ~60 s of polling |
| Boundary chain, wiki enrichment, building sweep | Celery, default queue | Minutes |

**The queue matters more than it looks.** The default Celery worker does not
consume `panel_fetch`. A deployment running `celery-worker` but not
`celery-worker-panels` shows every gallery and panel pending forever, and nothing
in the UI says why. Rule that out before reading a media failure as missing data.

Media has its own trap: the gallery's pending loaders poll with
`hx-trigger="load delay:2s"`, so `waitForHtmxSettled` passes straight through the
gaps between polls and asserts on an empty grid. Count `.media-provider-loader`
down to zero instead, which is what `settleGallery` in `hrsh-media.spec.ts` does.

## What the first run found

Written and then run against a real deployment rather than left untried. The
headline finding is recorded in `docs/PROBLEMS.md` under "a new pin never gets
its parcel": `create_pin_for_profile` stamps `Location.place_resolved_at` through
`resolve_location_place`, which never calls a provider, and every trigger for the
boundary chain reads that field as "already ran". The consequence is that a pin
on ground nobody has provisioned gets no parcel for `boundary_cache_days` (60).

That one defect is upstream of most of this directory, which is why the fixture
is built to report it once and skip the rest. It is now fixed, along with two
others found while confirming it - see `docs/PROBLEMS.md`.

### Presence is not provenance

The most instructive failure in this directory is one the original
`hrsh-boundary.spec.ts` could not produce. Every assertion in it passed while
the map drew a shape the application had **invented**: the convex hull of the
campus pin and its own child pins, rather than any of the six scored candidates
REData offers for the parcel. A boundary arrived, it was closed, it had real
vertices, it was plausibly sized, it contained the pin, and it was stable across
reads - and it was wrong.

That is the general trap in this directory, and it is worth stating plainly
because it will recur with owners, sale dates, aliases and media alike: *a
plausible value is not a sourced value*. Where the application can synthesize a
fallback, a test that only checks the shape of the answer will pass on the
fallback forever. `hrsh-boundary-provenance.spec.ts` is the counterpart that
asks where the value came from, and it is deliberately separate so the
distinction stays visible.

Its second test reconstructs the hull from live child-pin coordinates and
compares areas rather than hard-coding one. That was not fastidiousness: the
first version omitted the 10 m padding `_fitted_polygon` applies to each marker,
so its reconstruction came out materially smaller than the drawn shape and the
test **passed against known-broken data**. Any test written this way has to be
run against the defect it describes before it is trusted.

Two further things worth knowing before trusting a green run here:

- **The pinned-user count assertion is vacuous on this fixture.**
  `wiki_community_summary` counts pins on one `Location` row, while wiki *access*
  is by `Place.domain_root`. Five coordinates on this campus are five Locations
  of one pin each, so the "Fewer than 3" branch is reached no matter how many
  accounts pin the place. The assertion is kept because the copy is worth pinning
  down - the template hardcodes the threshold as a literal while
  `MIN_VISIBLE_PIN_COUNT` lives in Python - but it is not evidence the masking
  logic works.
- **The wiki is created automatically, and there is no draft state.**
  `tasks.ensure_wiki_for_location` creates it through
  `get_or_create_for_location` when its location gains a pin, and it is visible
  to anyone with access once that task has run; `GET wikis/<location_slug>/`
  answers 404 until then. `waitForCampusWiki` waits up to five minutes for it.
  Enrichment (`enrich_wiki_location`, the Wikipedia seed) runs later still, so
  assertions on the wiki's content must wait for it too.
