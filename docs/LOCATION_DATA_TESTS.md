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
python src/urbanlens/manage.py provision_integration_env --external-apis --out /tmp/e2e.json

cd tests/integration
UL_E2E_LOCATION_DATA=1 npx playwright test --project=location
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
2. **Bounds, not values.** The parcel's area is asserted to be between 200,000
   and 2,000,000 m² - a factor of three either side of the ~156 acres public
   reporting gives. It is not checking accuracy. It is catching the two failures
   `services/apis/locations/boundaries/redata.py` documents for this kind of
   site: a single building footprint selected instead of the parcel, and the
   ~1,040-acre CRIS archaeological sensitivity zone.
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

## Reading a failure

The directory is arranged so a broken pipeline produces **one** red, not thirty.
`specs/location/fixtures.ts` provisions the campus pin once per worker and never
throws; it carries either the geometry or a diagnosis. `hrsh-boundary.spec.ts` is
the single spec that reports missing geometry as a failure. Everything else calls
`campus.requireBoundary()` and skips with a pointer to it.

So: **read the boundary failure first.** If it is red, the skips below it are
consequences, not separate problems.

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
is built to report it once and skip the rest.

Two further things worth knowing before trusting a green run here:

- **The pinned-user count assertion is vacuous on this fixture.**
  `wiki_community_summary` counts pins on one `Location` row, while wiki *access*
  is by `Place.domain_root`. Five coordinates on this campus are five Locations
  of one pin each, so the "Fewer than 3" branch is reached no matter how many
  accounts pin the place. The assertion is kept because the copy is worth pinning
  down - the template hardcodes the threshold as a literal while
  `MIN_VISIBLE_PIN_COUNT` lives in Python - but it is not evidence the masking
  logic works.
- **The wiki is "created automatically" in a sense nobody can observe.** The
  draft is real (`ensure_draft_wiki_for_location`) but carries
  `officially_created=False`, and every visible surface treats that as "no wiki".
  The testable claim is not that the wiki appears, but that it is *already
  populated* the moment a user creates it - which is the only external evidence
  the background draft did its work.
