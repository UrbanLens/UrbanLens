# What the integration suite found that the unit suite did not

Every defect below was found by `tests/integration/` or `tests/contract/` against
a deployed instance, and none of them was failing in the ~12,000-test pytest
suite at the time. This document exists to turn that into work: for each one, why
pytest missed it, and what would have to exist for pytest to catch it next time.

The goal is **redundancy, not replacement**. The integration suite runs by hand
against staging and can go weeks between runs; anything it can teach the unit
suite to notice on every commit should be taught. What is left over - the things
that genuinely need a browser, a proxy, or two processes - is the honest
justification for keeping an integration suite at all.

Read the "Why pytest missed it" column before writing a test. Several of these
were missed for a *structural* reason, and a unit test written without
understanding that reason will pass while the defect stays.

---

## Already closed

These were found by integration and now have a pytest guard. Listed so nobody
writes a second one, and as worked examples of the shapes that turn out to be
testable.

| Finding | The guard now in pytest |
| --- | --- |
| `<html>` carried no `lang` on any page | `test_page_template_integrity.py::PageLanguageTests` - reads the template *source*, because the property does not depend on rendering |
| HTMX loaded from a CDN with no `integrity` | `test_page_template_integrity.py::SubresourceIntegrityTests` - asserted over *every* cross-origin `<script>`, not the one URL that was wrong |
| A fresh pin's panels 404'd because the reslug sweep moved its URL underneath an open page | `test_placeholder_slug_refresh.py::test_the_sweep_will_not_reslug_a_pin_somebody_may_be_looking_at`, plus a companion proving the guard is a delay and not an exemption |
| `DELETE /dashboard/e2ee/passkey-wrap/` raised `TypeError` out of the dispatcher (a 500) | `test_e2ee_passkey_unlock.py::test_delete_without_a_credential_id_is_refused_not_a_crash` |
| A visit could be logged in the future | `test_visit_time_bounds.py` - the serializer, the shared service, and the endpoint. **Defect fixed**, both layers |
| A photo upload trusted the filename over the bytes | `test_photo_bytes_must_be_an_image.py` - **defect fixed**; photos now need a positive image identification instead of sniffing failing open |
| Two pairs of operations shared an `operationId` | `test_published_schema_properties.py::OperationIdTests` - pure schema, no database |
| No authenticated operation documented its 401 | `test_published_schema_properties.py::DocumentedRefusalTests` - same file, also covers 403 and 404 |
| nginx advertised its exact version | `test_security_headers.py::ProxyConfigTests` - a static check over `nginx.conf`, which nothing had ever read |
| Responses did not match their declared schema | `test_response_matches_schema.py` - validates live responses against the generated document, plus five tests guarding the OpenAPI-3.0 `nullable` translation the check depends on |
| Re-adding a removed friend produced a request nobody could accept | `test_friendship_revival_direction.py` - **defect fixed**; covers the mute columns travelling with the ends, and the statuses that must refuse rather than revive |
| One pin-detail load fires 53 requests at once | `test_pin_detail_fanout_budget.py` - a ratchet on the count, not a reproduction of the exhaustion; see below for what it does and does not hold |

The pattern worth noticing: most of these became **static** checks over source or
a schema, not request/response tests. That is usually the cheapest way to close
an integration finding, and it is available more often than it looks.

**One of them nearly caused the regression it was meant to prevent**, which is
worth carrying forward. Making photo sniffing fail closed is only safe if every
allowed image extension has a magic-byte signature the library recognises - and
two did not agree by *name*: `filetype` reports a TIFF as `tif` and an animated
PNG as `apng`, and neither string was in the photo allowlist. Shipping the strict
check without noticing would have started rejecting genuine TIFF and APNG
uploads, trading a security hole for a broken feature. The fix carries alias
tests so it cannot be reintroduced. When you close one of the gaps below, ask
what the *stricter* rule now rejects that it should not.

---

## Closable - and now closed

All three are done. Kept in full because each one carries a reason worth reusing,
and because two of them are only *partly* closed in ways a future reader needs to
know before trusting them.

### 1. Responses did not match their declared schema

**Found:** `undo/` declared a bare array and returned `{entries, omitted}`;
`labels/` declared `location_count` required and omitted it. Both fixed.

**Why pytest missed it - and this is the important one:** the endpoint tests
assert against hand-written expectations, and *the same author writes the
serializer and the expectation*. Both can agree with each other while both
disagree with the published schema. No amount of adding endpoint tests in that
style closes this gap, because the gap is the style.

**Closed by** `test_response_matches_schema.py`, which runs on every `pytest`
invocation and validates a handful of representative responses against the
document drf-spectacular generates. `tests/contract/` does this far more
thoroughly - every operation, generated inputs, status codes and content types -
but lives outside `testpaths` and needs an explicit invocation, so the in-suite
file is the part that actually runs by default. Add an endpoint to its list
whenever one grows a response shape somebody could get wrong.

**The trap this one set, and it is a good one:** the document is OpenAPI **3.0**,
where nullable is spelled `{"type": "string", "nullable": true}`. `nullable` is
an OpenAPI keyword, not a JSON Schema one, so a plain validator ignores it and
rejects every null - and the first version of this test reported a confident
mismatch on *every nullable field in the API*. `next` and `previous` are null on
any single-page response, which is most of them. `openapi_to_json_schema()` folds
`nullable` into `type`, and five tests hold that the translation loosens exactly
that and nothing else, because a conversion that overshot would leave a test
passing against any response at all.

### 2. Re-adding a removed friend creates a request nobody can accept

**Found:** `DELETE friends/{uuid}/` soft-deletes to status `Removed`, and a
later `POST friends/` revived that row without re-orienting it - so the request
was recorded in the *old* direction and the recipient's accept 404'd. Both people
saw a request neither could act on, permanently.

**Why pytest missed it:** every friendship test starts from a clean slate. The
defect needs a *prior* relationship in a specific end state, which a test that
builds its own fixtures from nothing never has. This is the same blind spot as
the adversarial-input group, in a different disguise: not an input nobody tried,
but a *starting state* nobody started from.

**Fixed** in `Friendship.request`, and guarded by
`test_friendship_revival_direction.py`. Two things about the fix are worth
carrying:

- `unique_together` is `(from_profile, to_profile)`, which permits A->B and B->A
  to both exist, so swapping a row's ends can collide with a real row. The fix
  prefers an already-correctly-oriented row when one exists rather than swapping
  blind.
- `muted_by_from_profile` / `muted_by_to_profile` are **positional**. Swapping
  the ends without swapping these hands one person's mute to the other -
  silencing the wrong party, and invisibly. That has its own test.

This entry previously suggested generalising to the other statuses
`between()` can return - `Declined`, `Ignored`, `Blocked`. That was half wrong,
and the correction is the useful part: `can_request` admits `Declined` and
`Removed` only, so for `Blocked` and `Ignored` the right behaviour is a
**refusal**, and re-orienting one of those rows would have been a new defect.
Both now have tests asserting the refusal, sitting next to the re-orientation so
nobody widens one without reading why the other is narrow.

### 3. One Private Pin page load opens ~30 database connections at once

**Found:** the panel fan-out exhausted `max_connections`, producing 500s across
whichever panels arrived when the pool was full.

**Why pytest missed it:** each panel view is tested alone. The defect is a
property of *how many run at once*, which does not exist in a suite that issues
one request at a time.

**Partly closed** by `test_pin_detail_fanout_budget.py`, and the word *partly* is
load-bearing. It renders the page and counts the elements that fetch on load. It
does **not** reproduce the exhaustion - that needs concurrency against a real
pool, and stays in the integration-only table below. What it holds is the number,
which is the cause, and which creeps up one innocuous panel at a time.

Two things a reader needs before trusting it:

- The measured count is **53**, not the ~30 the deployment showed. The
  difference is real rather than an error in either: some triggers carry a filter
  (`load[!window.ulSectionCollapsed(...)]`) and stay quiet for a collapsed
  section. 53 is the ceiling a user with everything expanded reaches, which is
  the right thing to bound.
- The ceiling is set **at** the current count, so it is a ratchet, not an
  endorsement. 53 is already more than the dev deployment could serve at once.
  The budget stops it growing while the real fix - loading panels in waves, or
  behind one request - is decided. Lower it when that happens; raising it should
  take an argument.

The file carries a second test asserting the regex matches more than a handful of
elements. A ceiling test whose pattern silently matched nothing would pass
forever, which is the failure mode this shape is most prone to: it looks green
either way.

---

## Integration-only, and that is the point

Nothing in pytest will catch these. They are the standing argument for the
integration suite, and each should be understood rather than periodically
re-attempted.

| Finding | Why it cannot be a unit test |
| --- | --- |
| `div.app-nav-right` overflows a 390px viewport by 85px | Needs a layout engine. There is no way to compute a rendered box model from a Django template. |
| `image-alt`, `button-name`, `aria-required-children`, `link-in-text-block` | axe needs a real DOM, and two of these only appear *after* JavaScript runs or after an image fails to load - neither exists in a template test. |
| The database connection pool actually running out | Needs concurrency against a real pool. See the fan-out budget above for the half that is testable. |
| No `Strict-Transport-Security` from the TLS terminator | The header is not sent by Django at all; it belongs to infrastructure in front of it. |
| A wiki cannot be created through the published API | Is not a defect in code that runs - it is a *missing* endpoint. Only a test written from the client's seat, asking "can I do the thing", notices an absence. |
| The map drew a boundary we invented while REData offered six | The defect is a *disagreement between our cache and a live provider*. A unit test supplies the provider's answer itself, so it can only confirm the arrangement it already assumes. Nothing in-process can notice "REData had six candidates and we drew none of them". |

---

## When the fallback is the bug

Worth separating from the adversarial-input class below, because the remedy is
different and it has now cost two sessions.

The boundary defect (`docs/PROBLEMS.md`, "the map drew a hull around our own
child pins") was invisible to every existing test, unit and integration alike,
and not because anyone was careless. `hrsh-boundary.spec.ts` asserted that a
boundary arrives, that it is a closed polygon with real vertices, that it is
plausibly sized, that it contains the pin, and that it is stable across reads.
All five passed. The shape was a convex hull the application had fitted around
its own child pins.

The pattern generalises to every place this codebase synthesizes a fallback -
the 50 m default circle, a name derived from a geocode, an owner inferred from
the most recent sale. **Where a fallback exists, asserting the shape of the
answer will pass on the fallback forever.** The assertion has to be about
*provenance*: which source produced this, not whether the value looks right.
`resolve_for_pin` returns exactly that as its second element, and the boundary
payload publishes it as `source` - it simply was not being asked about.

Two practical consequences:

- When adding a location-data assertion, check whether the value under test has
  a synthesized fallback. If it does, assert the source alongside the value.
- A test that reconstructs what the application would have produced (to prove it
  did *not* produce it) must be run against the real defect before being
  trusted. The first version of the provenance check omitted a 10 m padding
  constant and passed against known-broken data.

---

## The lesson underneath most of these

Most of the findings above are **adversarial inputs nobody tried**: a
future date, a file that lies about its type, a method with no id, an operation
with no error responses. The unit suite is thorough about the feature working and
thin about the feature being abused, because tests get written alongside the code
they describe and by the same person.

The friendship one is the same blind spot wearing a different coat, and worth
separating out because it needs a different remedy. Nothing about that input is
adversarial - it is an ordinary request from an ordinary user. What is unusual is
the *starting state*: a relationship that already existed and was removed. A
suite that builds every fixture from nothing never begins there, so no amount of
adversarial input generation would have found it. Closing that class means
writing tests that start from states the system has *been* in, not only from
states it can be constructed in.

That is worth acting on more broadly than these instances. The suite already has the
machinery to do it systematically - Hypothesis is used throughout, and
`tests/contract/` fuzzes the API against its own schema. Pointing property-based
tests at *validation boundaries* rather than only at pure functions would close
this class rather than these instances.
