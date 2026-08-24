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

The pattern worth noticing: three of the four became **static** checks over source
or a schema, not request/response tests. That is usually the cheapest way to
close an integration finding, and it is available more often than it looks.

---

## Closable, and worth closing

Ranked by how cheap the pytest guard is against how bad the defect is.

### 1. A visit can be logged in the future

**Found:** `POST pins/{slug}/visits/` accepts `visited_at` a week from now and
answers 201, corrupting "last visited" everywhere it is displayed and ordered by.

**Why pytest missed it:** nothing asserts the *absence* of validation. Every
existing visit test supplies a sensible timestamp, because the author writing the
test is thinking about the feature working. Nobody wrote the adversarial case.

**What closes it:** a serializer-level test - no HTTP needed. Assert the field
rejects a future datetime and accepts a past one, then bound the field. This is
the cheapest guard in this document and the defect is a data-integrity one, so it
should be first.

### 2. A photo upload trusts the filename rather than the bytes

**Found:** a shell script uploaded as `not-really.png` with
`Content-Type: image/png` is stored and served back as an image.

**Why pytest missed it:** the upload tests all upload real images. The same
blind spot as above - the adversarial input was never tried.

**Watch out:** the integration test that found this *nearly missed it too*. The
first version reused the same payload, so from the second run onward the store's
duplicate detection answered 409, which reads as "refused". **A pytest guard must
use unique bytes per run, or assert on a fresh database.** Getting this wrong
produces a test that passes for the wrong reason.

**What closes it:** a `SimpleTestCase`-adjacent upload test posting non-image
bytes under an image name and asserting a 4xx, plus one asserting a real image
still succeeds so the check cannot be satisfied by rejecting everything.

### 3. Two pairs of operations shared an `operationId`

**Found:** `passkey_wrap_create`/`_destroy` were each claimed by two routes;
drf-spectacular resolved it by appending `_2` to whichever it walked second, so
adding an unrelated route could rename a method in every generated client.

**Why pytest missed it:** the schema tests that exist assert that *particular*
paths are present or absent. Nothing asserted a global property of the document.

**What closes it:** a pure-schema `SimpleTestCase` - generate the document with
`SchemaGenerator().get_schema(request=None, public=True)` and assert every
`operationId` is unique. No database, no HTTP, runs in seconds. The assertion
already exists in `tests/contract/test_openapi_conformance.py`; it should be
*mirrored* into the pytest suite, because `tests/contract` is outside `testpaths`
and does not run on a normal `pytest` invocation.

### 4. No authenticated operation documented the 401 it returns

**Found:** 284 operations declared `security` and documented only success, so a
generated client had no branch for the most likely failure it will meet.

**Why pytest missed it:** same reason as above - no test asserted a property
across all operations.

**What closes it:** the same pure-schema test file. Assert that an operation
declaring `security` documents 401 and 403, and that a path with a parameter
documents 404. Cheap, and it now passes, so it is a regression guard rather than
a backlog item.

### 5. Responses did not match their declared schema

**Found:** `undo/` declared a bare array and returned `{entries, omitted}`;
`labels/` declared `location_count` required and omitted it.

**Why pytest missed it - and this is the important one:** the endpoint tests
assert against hand-written expectations, and *the same author writes the
serializer and the expectation*. Both can agree with each other while both
disagree with the published schema. No amount of adding endpoint tests in that
style closes this gap, because the gap is the style.

**What closes it:** `tests/contract/` already does exactly this, in-process,
needing no deployment. The gap is that it is not part of the default run. Either
add a fast subset to CI, or accept that this class is caught only when somebody
runs the contract suite - and say so out loud rather than assuming coverage.

### 6. nginx advertised its exact version

**Found:** `Server: nginx/1.31.3`.

**Why pytest missed it:** the config file is not Python and nothing reads it.

**What closes it:** a static check over `src/urbanlens/config/nginx/nginx.conf`
asserting `server_tokens off;` is present in the `http` block - the same shape as
`test_page_template_integrity.py`. Cheap, and it generalises: that file is
otherwise untested.

### 7. One pin-detail page load opens ~30 database connections at once

**Found:** the panel fan-out exhausted `max_connections`, producing 500s across
whichever panels arrived when the pool was full.

**Why pytest missed it:** each panel view is tested alone. The defect is a
property of *how many run at once*, which does not exist in a suite that issues
one request at a time.

**What partly closes it:** a **fan-out budget** test - render the pin detail
template and assert the number of elements that will fire an HTMX request on load
stays under an agreed ceiling. That does not reproduce the exhaustion, but it
catches the thing that causes it, which is the count creeping up. It is a static
count over rendered HTML and is the only part of this finding a unit test can
hold.

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

---

## The lesson underneath most of these

Six of the closable findings above are **adversarial inputs nobody tried**: a
future date, a file that lies about its type, a method with no id, an operation
with no error responses. The unit suite is thorough about the feature working and
thin about the feature being abused, because tests get written alongside the code
they describe and by the same person.

That is worth acting on more broadly than these six. The suite already has the
machinery to do it systematically - Hypothesis is used throughout, and
`tests/contract/` fuzzes the API against its own schema. Pointing property-based
tests at *validation boundaries* rather than only at pure functions would close
this class rather than these instances.
