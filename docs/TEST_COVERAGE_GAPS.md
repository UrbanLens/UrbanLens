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

## Closable, and worth closing

Ranked by how cheap the pytest guard is against how bad the defect is.

### 1. Responses did not match their declared schema

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

### 2. Re-adding a removed friend creates a request nobody can accept

**Found:** `DELETE friends/{uuid}/` soft-deletes to status `Removed`, and a
later `POST friends/` revives that row without re-orienting it - so the request
is recorded in the *old* direction and the recipient's accept 404s.

**Why pytest missed it:** every friendship test starts from a clean slate. The
defect needs a *prior* relationship in a specific end state, which a test that
builds its own fixtures from nothing never has. This is the same blind spot as
the adversarial-input group, in a different disguise: not an input nobody tried,
but a *starting state* nobody started from.

**What closes it:** a pytest test that sets a `Friendship` to `Removed` first,
then sends a request the other way and asserts the recipient can accept. Cheap -
it is one service-level test with no HTTP - and worth generalising to the other
statuses `Friendship.objects.between()` can return (`Declined`, `Ignored`,
`Blocked`), each of which a later request will find and reuse.

### 3. One pin-detail page load opens ~30 database connections at once

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

Most of the closable findings above are **adversarial inputs nobody tried**: a
future date, a file that lies about its type, a method with no id, an operation
with no error responses. The unit suite is thorough about the feature working and
thin about the feature being abused, because tests get written alongside the code
they describe and by the same person.

The friendship one (6) is the same blind spot wearing a different coat, and worth
separating out because it needs a different remedy. Nothing about that input is
adversarial - it is an ordinary request from an ordinary user. What is unusual is
the *starting state*: a relationship that already existed and was removed. A
suite that builds every fixture from nothing never begins there, so no amount of
adversarial input generation would have found it. Closing that class means
writing tests that start from states the system has *been* in, not only from
states it can be constructed in.

That is worth acting on more broadly than these six. The suite already has the
machinery to do it systematically - Hypothesis is used throughout, and
`tests/contract/` fuzzes the API against its own schema. Pointing property-based
tests at *validation boundaries* rather than only at pure functions would close
this class rather than these instances.
