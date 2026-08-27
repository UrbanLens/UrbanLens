# Contract tests

Property-based conformance testing of the external API against its own published
OpenAPI document, using [schemathesis](https://schemathesis.readthedocs.io).
Lives in `tests/contract/`, driven by `bin/run_contract_tests.sh`.

## Why this exists

The schema at `/dashboard/api/external/v1/schema/` is not a build artefact. It is
served to third parties, clients are generated from it, and the Flutter app is
one of them. Every difference between that document and the running code is a
break in somebody else's build, found by them.

Nothing else checks it:

- The Python suite asserts endpoint behaviour against hand-written
  expectations. The same author writes the serializer and the expectation, so
  the two can agree with each other while both disagree with the schema.
- The Playwright `api` project checks that the document *generates*, that it
  does not leak `/dashboard/rest/`, and that the paths it depends on are
  present. It never compares a response body to the schema.

Schemathesis reads the document as a specification, generates requests from it
with Hypothesis, and holds the responses to what was declared.

## Quick start

```bash
# In-process: no deployment, no server. Needs a database.
bin/run_contract_tests.sh

# Against a deployment. Needs no database.
bin/run_contract_tests.sh --url https://s1.dev.urbanlens.org

# Search harder, or include writes.
bin/run_contract_tests.sh --examples 40
bin/run_contract_tests.sh --methods all
```

`--local` runs in the host venv instead of the test container. Anything after
`--` goes to pytest (`-- -k pins -x`).

## The two modes

| | In-process (default) | Live (`--url`) |
| --- | --- | --- |
| Schema from | drf-spectacular, direct | the deployment's `schema/` |
| Requests go to | Django's WSGI callable | over HTTP, through the proxy |
| Needs | a test database | a deployment and an API key |
| Answers | does the code match the schema | does what we *shipped* match it |

In-process generates the document rather than fetching it over WSGI. A fetch is
a request, and a request means middleware, `ALLOWED_HOSTS` and possibly the
database — all at collection time, before pytest has set anything up.
drf-spectacular's generator is pure introspection and runs the same
preprocessing hooks the view does, so the document is identical and cannot fail
for reasons unrelated to the schema.

## What it checks

Per operation, on every generated request:

- **`not_a_server_error`** — no 500.
- **`response_schema_conformance`** — a returned body validates against the
  schema declared for that response.

Plus three assertions about the document itself, which need no requests:

- every `operationId` is unique,
- every operation declares at least one response,
- authenticated operations declare `security`, and document a 401.

## What it deliberately does not do

**Undocumented status codes and content types** are not failures by default.
They are real checks and this schema is not ready for them: it documents only
success, so a GET on `pins/{pin_slug}/` with a generated slug correctly returns
404 and the check fires on a correct response. Almost every parameterised
operation would be red for the same reason. `UL_CONTRACT_STRICT=1` turns them
on; the `test_authenticated_operations_document_rejection` document check
measures how far off the schema is.

**`ignored_auth`** is excluded permanently. It re-sends a request with the
credential removed or corrupted, but it can only tamper with a credential *it*
generated — and this suite supplies a real one outside that model, so the check
watches its own "invalid" request succeed and reports a bypass that is not
there. Real auth rejection is covered by the Playwright suite's
`api/auth.spec.ts`.

**The coverage phase** is off by default (`UL_CONTRACT_COVERAGE=1` enables it).
It is the part that drops required headers, sends undeclared methods and
mistypes parameters. All useful, all currently answered with an undocumented
status, so it produces one systemic failure per operation. Worth turning on
deliberately when auditing error responses.

**Detail operations mostly exercise the 404 path.** Path parameters are
generated, so `pins/{pin_slug}/` is asked for a slug that does not exist. The
collection endpoints get real coverage; the detail endpoints currently prove
only that a miss is handled. Seeding real identifiers through
`ProjectConfig(parameters=...)` is the obvious next step and the single biggest
increase in value available here.

## Configuration

| Variable | Meaning |
| --- | --- |
| `UL_CONTRACT_BASE_URL` | Deployment to test. Unset means in-process. |
| `UL_CONTRACT_METHODS` | `safe` (default, GET/HEAD) or `all`. |
| `UL_CONTRACT_MAX_EXAMPLES` | Examples per operation. Default 8. |
| `UL_CONTRACT_STRICT` | Also fail on undocumented status/content type. |
| `UL_CONTRACT_COVERAGE` | Run the negative coverage phase. |
| `UL_CONTRACT_API_KEY` | Live mode credential. |
| `UL_E2E_ACCOUNTS_FILE` | Live mode fallback: the manifest `provision_integration_env --out` writes. |
| `UL_TEST_DB_NAME` | In-process; required so concurrent runs do not collide. |

In-process, the account is created by the same
`services.integration_testing.accounts` code the Playwright suite's management
command uses, so both suites authenticate as the same kind of user.

## Three traps, already paid for

**Never unblock the database by hand.** `django_db_blocker.unblock()` removes
pytest-django's guard without setting a test database up, so every query goes to
whatever `DATABASES["default"]` names — on a developer's machine, their actual
dev database.

**`request.getfixturevalue("db")` does not work, and does not say so.** The call
returns, nothing raises, and the connection is still pointed at the default
database. Declaring `db` as a parameter, or applying the `django_db` marker,
sets the test database up; fetching it dynamically does not.
`conftest._assert_on_a_test_database` exists so a third variation of this
mistake stops the run instead of writing to the wrong database.

**A raw WSGI callable is not Django's test client.** The test client disconnects
`close_old_connections` from `request_started` before invoking the handler.
Nothing does that here, and a request that closes the connection discards the
transaction holding the fixtures. The symptom is not an error: the account is
created, the request runs, and the API answers `401 Authentication credentials
were not provided`, because by the time it looks the key is gone.

## What the first run found

**Both fixed 2026-08-24** - kept here as the reason `test_operation_ids_are_unique` and
`test_authenticated_operations_document_rejection` exist, not as open findings. Full history in
`docs/PROBLEMS.md`.

- **`operationId` collisions.** `passkey_wrap_create` and `passkey_wrap_destroy` were each
  claimed by two operations (`/dashboard/e2ee/passkey-wrap/` and `.../{credential_id}/`).
  drf-spectacular resolved this by appending `_2` to whichever it reached second, and which one
  lost depended on urlconf walk order — so adding a route could silently rename a method
  downstream code calls. Fixed by splitting the shared view into `E2EEPasskeyWrapView` (POST
  only) and `E2EEPasskeyWrapItemView` (DELETE only) over one `_E2EEPasskeyWrapBase`
  (`controllers/e2ee.py`), so no operation claims two routes.
- **No authenticated operation documented a 401**, though all of them returned it. A generated
  client had no branch for the most likely failure it would meet. Fixed by a postprocessing hook,
  `external_api.schema.document_error_responses`, that `setdefault`s 401/403 on any operation
  declaring `security` and 404 on any templated path.
