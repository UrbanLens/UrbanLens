# Resolved problems (archive)

Entries from `docs/PROBLEMS.md` whose headers record them as resolved, fixed or dismissed,
moved here on 2026-08-18 so the live file lists what still needs attention.

Kept rather than deleted: several of these are the only written record of *why* something is
shaped the way it is, and a few document traps that would otherwise be rediscovered the hard
way. Search here before concluding a defect is new.

Note for anything citing this material by line number: `docs/reports/` contains audit reports that
quote `PROBLEMS.md:<line>`. Those numbers refer to the pre-split file and now point at different
content - follow them by *searching for the quoted text*, not by jumping to the line.

## RESOLVED 2026-08-12: a 225 KB generated source map was tracked while its stylesheet was ignored

`.gitignore` ignores `**/frontend/static/**/*.css`, which does **not** match `.css.map` - so
`static/dashboard/style.css.map` (225 KB, last committed 2026-08-04) was tracked while
`style.css` itself was not. Consequences: every `bun run sass:dev` dirtied a committed artifact
(this bit me mid-audit), and the map was useless anyway - the stylesheet it maps is never
committed, the production `sass` script passes `--no-source-map` so releases never produce one,
and no template references it.

Untracked it and extended the ignore rule to `.css.map`. Verified afterwards: `sass:dev` leaves
the tree clean, the file remains on disk for local debugging, and `bun run sass` still emits no
map.

Checked the neighbours rather than assuming: the five tracked `static/js/*.js` files are
hand-written (JSDoc headers, "Usage:" docs) and live outside `bin/build-frontend.ts`'s output dir
(`static/<app>/js/`), so they are correctly tracked - the `.gitignore` comment already explains
that distinction.

## RESOLVED 2026-08-12: 4 unused Python runtime dependencies (pulling scipy) removed

Same audit as the JS manifest, applied to `pyproject.toml`'s 71 runtime dependencies. Resolving
each distribution's real import names from installed metadata (naive `name.replace("-","_")`
mis-reports `pillow`→`PIL`, `djangorestframework`→`rest_framework`, `psycopg2-binary`→`psycopg2`
and a dozen others) and searching the whole repo, then cross-checking which are required by
another installed distribution:

**Used indirectly, correctly declared** - `psycopg2-binary` and `psycogreen` (`gunicorn.conf.py`),
`pyyaml` (`src/bin/`). A DB driver is never imported by application code.

**Redundant but harmless** - `django-auto-prefetch`, `django-dirtyfields`, `django-pandas`,
`django-picklefield` (all required by `djangofoundry`, which is itself a declared dep), plus
`jinja2`, `linkify-it-py`, `orjson`, `python-dateutil`, `simplejson`, `sqlalchemy`. Left alone:
declaring a transitive dep explicitly is a defensible choice, and removing them changes nothing
about what gets installed.

**Referenced by nothing, and required by no installed distribution** - removed:
`django-extensions` (a dev tool, and not even in `INSTALLED_APPS`), `esprima`, `python-decouple`
(the project uses pydantic-settings and `os.getenv`), and `statsmodels`. Removing `statsmodels`
also dropped **`scipy`** and `patsy` transitively; verified nothing imports any of them.

Worth noting `django-extensions` was the only entry a keyword scan for dev-tooling flagged, and it
turned out to be entirely unused rather than merely misplaced.

**Validated with the packages genuinely absent.** The test container has no `pip` but does have
`uv`, so `uv sync --frozen` against the updated lockfile actually removed them from its venv -
confirmed by importing each and getting `ModuleNotFoundError`. The full suite then ran on a fresh
database in that environment: **10,285 passed, 0 failed** (1h19m). Together with the static
evidence (nothing in the repo references them; no installed distribution requires them), the
removal is safe.

## RESOLVED 2026-08-12: `bun run sass` crashed with ERR_REQUIRE_ESM, and 5 dead deps shadowed system tools

Follow-up to the pinned-`bun` finding below - same mechanism, three more instances.

**`bun run sass` failing** (documented at length in `CLAUDE.local.md` as a host quirk with a
manual workaround) has the same shape: `bun run` resolves `sass` to `node_modules/.bin/sass`,
whose `#!/usr/bin/env node` shebang hands execution to the system Node. On Node 18 the bundled
sass `require()`s chokidar, which is ESM-only, so it dies with `ERR_REQUIRE_ESM`. Fixed by
pointing the three `sass*` scripts at `bun node_modules/sass/sass.js` instead of the shim, so
Bun's own runtime executes it. `bun run sass` now produces the stylesheet (958 KB compressed);
`sass:dev` works too. **`CLAUDE.local.md`'s "sass gotcha" section is now stale** - the manual
`bun node_modules/.bin/sass ...` workaround it prescribes is no longer needed.

**Five dependencies were declared but never used anywhere** - verified by searching every `.ts`,
`.js`, `.json`, `.toml`, `.yml`, shell script and Dockerfile: `yarn`, `sass-loader`, `semver`,
`dotenv`, `dotenv-expand`. `yarn` is the same anti-pattern as the pinned `bun` (a package manager
as a runtime dependency, putting `yarn`/`yarnpkg` on `PATH` for every `bun run`); `sass-loader` is
a *webpack* loader in a project that bundles with Bun. Removed.

**Two classification errors**, which matter because `dependencies` is what a production install
pulls: `typescript` and `sass` are build tools and were in `dependencies` (so `tsc` shipped to
production); `sortablejs` is imported by three source modules but sat in `devDependencies`.
Swapped.

Verified after all of the above: `bun run typecheck` clean, `bun run test:ts` **383 pass / 0
fail**, `bun run build` OK, `bun run sass` OK. The `.bin` directory now contains only
`markdown-it`, `sass`, `tsc`, `tsserver` - all genuinely used.

Minor inconsistency noticed, not changed: `static/dashboard/style.css` is gitignored but
`style.css.map` is *tracked*, so a `sass:dev` run dirties a committed artifact whose source isn't
committed.

## RESOLVED 2026-08-12: a passing test was asserting against a *failed* import, hiding a live network call

Found by surfacing `ERROR`-level logs from **passing** tests (`-o log_cli=true
--log-cli-level=ERROR`) - a signal the suite normally hides, since the custom runner suppresses
logs unless a test fails.

`test_import_preview_streaming.py::ImportPreviewDescriptionExtrasTests::
test_html_is_stripped_from_the_saved_description` passes an `<img src="https://example.com/a.jpg">`
in the description. That makes the importer materialize the photo, which **fetches the URL**. The
suite's network guard raises `RuntimeError`; `import_preview_streaming` catches
`(DatabaseError, OSError, ValueError, RuntimeError)`, logs "Unexpected error during preview
import", and yields `Import failed unexpectedly`. The test still passed - the pin had already been
created by that point, so its assertions about the stripped description held **against a failed
import**.

Two problems in one: the suite attempted a real outbound request on every run, and a test that
reads as covering the happy path was in fact exercising the error path. The sibling test
`test_img_src_becomes_a_pin_photo_not_a_link` already mocks `materialize_media_item`; this one now
does the same. 21 passed, and the ERROR is gone.

Swept the rest of the import surface the same way afterwards: `-k 'import or preview or takeout'`
→ **438 passed, zero** `External network access is disabled` occurrences.

**Worth reusing**: `except (…, RuntimeError)` around a broad block will swallow the network
guard's own exception, so an unmocked integration shows up as a passing test plus a log line
rather than a failure. Grepping ERROR logs across passing tests is the way to find the rest.

## ~~FEATURE GAP 2026-08-11: the data export omits 11 kinds of user-authored content~~ MOSTLY RESOLVED 2026-08-15 (`a2743a29`)

**RESOLVED for 9 of the 11**, via a declarative `ExportType` registry rather than nine more
copy-pasted exporters: `VALID_EXPORT_TYPES`, the run order and `run_export`'s dispatch table all
derive from one tuple, so a tenth area is a class plus one entry. New areas: **safety_checkins**
(check-ins + contacts + messages), **map_annotations** (MarkupMap/PinMarkup/MapImageOverlay incl.
overlay image files), **saved_filters**, **routes**. **PinAlias** folds into the per-pin dicts;
**SocialLink** and **ProfileEmail** fold into the profile. The reverse-direction gap is closed too
- there is now a `_import_profile` restoring content fields only (bio/area/dates/contact block),
explicitly skipping username/email/date_joined.

Registry entries also carry a `label`/`description`, and the Tools page renders them from the
registry, so a future export area needs no template edit - the four new areas were otherwise
unreachable in the UI, since that checkbox list is hardcoded.

**Still deliberately excluded, pending a product decision** (the two this entry itself flagged):
`ProfileNote` (a note *about* another user - exporting one user's private characterization of
another is a real disclosure question) and `WikiEdit` (community-shared revision history, not
solely the exporter's content). Recommendation if asked: export ProfileNote but never import it,
and leave WikiEdit alone. Original entry below.

`VALID_EXPORT_TYPES` covers 13 areas (profile, settings, custom fields, pins, google_takeout,
labels, connections, visit history, comments, photos, trips, pin lists, direct messages). Pins
carry their `article` inline, so long-form content *is* included.

Checked every `dashboard` model that holds user-owned rows via a `profile`/`user`/`author`/
`created_by`/`sender` FK (103 of them) against what `export.py` actually reads. Most of the
difference is correctly omitted - see below - but these are user-authored content with no
representation in the archive at all:

| missing | what the user loses |
|---|---|
| `SafetyCheckin` (+ contacts, messages) | every safety plan they ever wrote |
| `MarkupMap`, `PinMarkup`, `MapImageOverlay` | hand-drawn map annotations and overlays |
| `SavedFilter` | saved searches |
| `Route` | saved routes |
| `PinAlias` | alternate names they gave their own pins |
| `ProfileNote` | private notes they wrote about other people |
| `SocialLink` | profile links |
| `ProfileEmail` | secondary addresses |
| `WikiEdit` | their contributions to community wikis |

Verified genuinely absent, not nested: the only `markup`/`alias` strings in `export.py` are
*profile preference* fields (`markup_fill_color`, `sync_aliases`), not the content models.

**Why this is worth more here than in a typical app**: the FAQ makes data ownership an explicit
product promise - "On Google Maps, you don't own your data, and it's clunky to export any of it
... which makes me uncomfortable" (`pages/faq/index.html:50`). An export that silently drops a
user's entire safety-check-in history undercuts that claim specifically.

**Correctly omitted, do not "fix" these**: credentials and key material (`TOTPDevice`,
`WebAuthnCredential`, `MessagingKeyBundle`, `GroupKey`, OAuth token rows) must never appear in an
archive the user downloads and may forward; derived/system bookkeeping (`LocationExposure`,
`PinTombstone`, `SearchHistory`, `ProfileActivityDay`, `ProfileStreak`, `UndoAction`) is not
user-authored and mostly meaningless outside the app.

**Two need a decision before implementing**, not just an exporter: `ProfileNote` is a private note
*about another person* (and encrypted at rest), and `WikiEdit` is community content the user
authored but does not solely own. Both are defensible either way; neither should be added on
autopilot.

**Mostly resolved (chunk 469, 2026-08-15).** New export types `safety` (check-ins with contacts
and messages nested; contact-portal tokens deliberately omitted from a forwardable archive),
`map_annotations` (markup maps with their shapes, image overlays), `saved_searches` (saved
filters + routes as GeoJSON); `PinAlias` rides inside each pin's row; `SocialLink` and
`ProfileEmail` ride in profile.json, with social links imported back (idempotent on
platform+handle) and secondary emails deliberately NOT imported (verification state is an
account-security decision). UI checkboxes added. `ProfileNote` and `WikiEdit` remain
decision-gated, exactly as this entry argued.

### The round trip is also lossy in the other direction: `profile` is exported but never imported

`export.py` writes 13 types; `import_data.py`'s `_IMPORTERS` (and `_IMPORT_ORDER`) handle 11.
`google_takeout` is an *output format*, correctly not re-imported. **`profile` is the real gap.**

`_export_profile` writes `bio`, `area`, `birth_date`, `started_exploring` and the entire contact
block (`phone_number`, `signal_username`, `discord_username`, `whatsapp_number`,
`telegram_username`, `matrix_handle` - the fields encrypted at rest in migration 0039), alongside
identity fields. Nothing reads any of it back: there is no `profile` importer, and `_import_settings`
covers privacy/community/notification preferences only. So a user who exports and re-imports gets
their pins, photos and trips back but silently loses their bio, area and every contact handle -
data they can *see* sitting in their own archive.

Skipping `username`/`email`/`date_joined` on import is obviously right (importing into a different
account must not overwrite its identity). The content fields are a different question and look
like an omission rather than a decision - there is no comment either way, and `settings`, which is
equally account-level, *is* imported.

Not a UI problem, checked: `_IMPORT_ORDER` doesn't list `profile`, so no misleading "Importing
profile..." step is ever shown - it is simply absent.

**Resolved (chunk 467, 2026-08-15): the round trip closes.** `_import_profile` restores bio,
area, the dates, first/last name and every contact handle; identity (username/email/date_joined)
stays untouched by design - an archive must not overwrite the login identity of the account it
is imported into. Absent keys leave current values alone, so pre-gap archives blank nothing.
Round-trip, identity-protection and pre-gap-archive behavior pinned by `test_import_profile.py`.
The 7 uncontroversial missing *export* kinds (safety plans, markup, saved filters, routes,
aliases, social links, secondary emails) remain open above; ProfileNote/WikiEdit still need the
decision the entry describes.

## ~~LOW 2026-08-11: one notification preference is named after the enum *member*, not its *value*~~ RESOLVED (verified 2026-08-15)

**RESOLVED**: the trap is closed, not merely dormant. `_enabled_channels`
(`notification_text_alerts.py:132-133`) now derives the preference prefix from the enum *member
name* (`NotificationType(...).name.lower()`), which matches the `safety_checkin_partner_invite*`
columns, and `safety_ci_partner_invite` is now listed in `TEXT_ALERTABLE_TYPES`
(`notification_text_alerts.py:63`). Regression coverage:
`tests/hypothesis/test_text_alert_preference_stems.py` asserts `TEXT_ALERTABLE_TYPES` equals
exactly the stems with a toggle pair and that the mismatched type's toggles are read correctly;
`test_external_api_notifications.py:99` pins the known stem/value divergence. The suggested column
rename was not done, but no remaining code derives preference field names from the type *value*,
so the two lookup styles no longer need to agree. Original entry below for context.

`NotificationType.SAFETY_CHECKIN_PARTNER_INVITE` has the **value**
`"safety_ci_partner_invite"`, but its three preference columns on `NotificationPreference` are
named `safety_checkin_partner_invite`, `..._whatsapp`, `..._sms` - i.e. after the enum *member
name*. It is the only one of the 13 stems that doesn't equal a `NotificationType` value.

**Working correctly today**: `services/visits/safety.py:775` reads the field by its literal
attribute name (`partner.profile.notification_preferences.safety_checkin_partner_invite`), so
the site/email toggle is honoured, and the settings page renders it because
`preference_field_names()` introspects model fields rather than types.

**The trap is the WhatsApp/SMS path.** `notification_text_alerts.py:115` builds its field names
from the *type value*:

```python
prefix = notification.notification_type              # "safety_ci_partner_invite"
getattr(prefs, f"{prefix}_whatsapp", False)          # column is safety_checkin_partner_invite_whatsapp
```

with a `False` default. That path is currently unreachable for this type only because
`TEXT_ALERTABLE_TYPES` doesn't list it. Add it to that set - the obvious way to give partner
invites a text alert - and the lookup misses, `getattr` silently returns `False`, and the
user's toggle is permanently off with no error anywhere.

Fix if touched: rename the three columns to `safety_ci_partner_invite*` (a migration plus the
one read site above), so every stem equals its type value and both lookup styles agree.

## ~~LOW 2026-08-11: `FriendInvitation.mark_accepted` claims at selection time, not write time~~ RESOLVED 2026-08-15

**RESOLVED**: `mark_accepted()` is now a write-time conditional claim
(`filter(pk=..., accepted_at__isnull=True).update(...) == 1`, returning bool, syncing the
in-memory instance on a won claim), and `_apply_pending_invitation` claims FIRST -
`if not invitation.mark_accepted(): return` - before `Friendship.request`/notify/grant.
Deliberately NOT wrapped in `transaction.atomic()`: `Friendship.save()` fires the achievements
post_save handler whose `active_metric_keys` catches bare `Exception` including `DatabaseError`,
which inside an atomic block would poison the transaction exactly as this doc's own NOTE at
line ~353 warns - so the accepted trade-off is a crash after the claim loses that invite's side
effects rather than double-applying them (documented in the controller docstring). 3 new tests in
`test_friend_invitation.py` (9/9 passing), including a stale-instance replay asserting zero side
effects. Original entry below.

`_collect_pending_invitations` (`controllers/account.py:995`) filters on
`accepted_at__isnull=True` and its docstring says that "already guards against reprocessing".
It guards at *selection* time only: `FriendInvitation.mark_accepted()`
(`models/friendship/invitation/model.py:65`) then writes `accepted_at` with an **unconditional**
`update()`, and the side effects in `_apply_pending_invitation` run *before* that write. Two
concurrent verifications of the same invite (a double-clicked verification link) can both select
it and both apply it.

**Currently harmless, which is why it was left alone**, and each reason is worth recording
because they are what a future change could remove:
- `grant_subscription` → `set_duration_months` sets an *absolute* `expires_at`
  (`now + months*30d`), so re-granting the same role recomputes the same expiry rather than
  stacking it.
- `Friendship.request` checks `between()` first and the model has
  `unique_together = ("from_profile", "to_profile")`, so no duplicate row survives; a true race
  raises `IntegrityError`, which is a `DatabaseError` and so is caught and logged by
  `_process_pending_invitations`.
- The residue is a possible duplicate `notify_friend_request` notification.

**The hazard is the docstring, not today's behaviour.** Anyone adding a side effect here that
*does* stack - a credit, a referral bonus, a duration top-up rather than a reset - would inherit
a silent double-apply while reading a comment that says reprocessing is already prevented. If
that happens, the fix is to make `mark_accepted()` a conditional claim
(`filter(pk=..., accepted_at__isnull=True).update(...)`, return whether it matched) and call it
*before* the side effects, accepting that a failure after the claim loses the grant.

Found during a sweep of read-then-unconditional-write single-use markers; every other one checked
(`BackupCode` after its fix, `SafetyCheckinPartner`, `PushDevice`, `ApiKey`, `UserSubscription`)
is either conditional or genuinely idempotent.

## ~~LOW 2026-08-11: the hourly DM retention sweep seq-scans, then materialises its whole result set~~ RESOLVED 2026-08-15 (batching; index still deliberately deferred)

**RESOLVED (the batching half)**: `hard_delete_expired_direct_messages()` now takes
`batch_size: int = 2000` and `max_per_run: int = 50000`, and slices the due-id query, bounding the
materialised list and every downstream `IN` clause. A backlog drains in batches *within* a run up
to `max_per_run`, and across runs beyond that, so neither the parameter limit nor the task's
runtime is unbounded. (Two implementations of this landed independently on parallel branches; the
merge kept the one with the per-run ceiling.) 3 new tests in
`test_direct_message_hard_delete.py` (20/20 passing). The partial index proposed below remains
deliberately NOT added - that stays a measured production decision per this entry's own
reasoning; the proposed index definition is preserved below for whoever measures. Original entry:

`DirectMessageQuerySet.due_for_hard_delete` (`models/direct_messages/queryset.py:98`) filters on
`sender_delete_after` + `read_at`. Confirmed against a real database - the only indexes on
`dashboard_direct_messages` are:

```
(id) pkey, (sender_id), (recipient_id), (markup_map_id), (reply_to_id),
(sender_id, recipient_id), (recipient_id, read_at),
(sender_id, client_uuid) WHERE client_uuid IS NOT NULL
```

Nothing leads with `sender_delete_after`, and `read_at` only appears as the *second* column of
`(recipient_id, read_at)`, which is unusable without a `recipient_id` predicate. So
`hard_delete_expired_direct_messages` (hourly, `settings/base.py:343`) sequentially scans the
entire direct-message table every hour, forever, and the scan grows with total history rather
than with the number of messages actually due.

**Deliberately not fixed here.** The right index is probably partial -

```python
Index(fields=["sender_delete_after", "read_at"], name="idxdb_dm_retention_sweep",
      condition=Q(read_at__isnull=False) & ~Q(sender_delete_after="never"))
```

- since `NEVER` and unread rows can never match, and a full index on a hot write table would
pay write amplification to serve one hourly reader. But whether it is worth *any* index depends
on production table size, which this environment can't measure: at beta volumes an hourly seq
scan is free. Migration 0038 (`drop_redundant_uuid_indexes`) shows indexes here are actively
curated, so this should be a measured decision, not a speculative addition.

The same question applies to the 120-second `sweep_stalled_*` session sweeps, which run 30x more
often; their tables are far smaller, but they're the ones to check first if sweep cost ever shows
up in profiling.

**Compounding factor found 2026-08-11 (chunk 25).** The task doesn't just scan - it materialises:

```python
due_ids = list(DirectMessage.objects.due_for_hard_delete().values_list("id", flat=True))  # tasks.py:2172
expiring = list(Image.objects.filter(direct_message_id__in=due_ids))                      # :2176
```

so every due id is pulled into memory and then sent back as one `IN (...)` list. In steady state
that set is small (one hour's worth of expiries). The dangerous moment is any time a *backlog*
becomes due at once - the first run after this sweep shipped, a retention-policy change, or a
period when the beat worker was down - where the `IN` list can reach the size of the expired
population and hit Postgres parameter/planning limits. Batching the id list (e.g. slices of a few
thousand per run, remainder picked up next hour, as `upgrade_placeholder_pin_names` already does
with `batch_size`) removes both this and the unbounded-runtime concern, independently of whether
the index is ever added.

**The materialisation half is fixed (2026-08-16).** `hard_delete_expired_direct_messages` now
takes `batch_size=2000` slices in a loop with a `max_per_run=50000` ceiling, so the `IN (...)` list
is bounded regardless of backlog size and one invocation cannot run unboundedly long; any remainder
is picked up by the next hourly run. One detail worth knowing before changing the batch size: a
stored file shared by `Image` rows in *different* batches survives the earlier batch (the later
batch's row still references it) and is removed by the later one, because the earlier batch's rows
are gone by then - so batching does not leak files, it just defers some of them by one iteration.
Covered by `test_direct_message_hard_delete.py::HardDeleteBatchingTests`.

**The index question below is untouched and is still the substance of this entry** - it needs
production table size, which this environment cannot measure.

## RESOLVED 2026-08-11: `--reuse-db` permanently poisons the test DB, breaking every OAuth test

**Symptom**: a run that passed yesterday fails today with
`oauth2_provider.models.Application.DoesNotExist: Application matching query does not exist.`
Running the affected files alone produced **98 failed / 3 passed**; the same files inside a
larger `-k` selection failed only 8. Reads like a product bug; is not one.

**Cause**: the `urbanlens-mobile` Application row is created by a *data migration*
(`0010_v0_6_0.py::create_first_party_client`). Django only guarantees migration-created data
for `TestCase`. A `TransactionTestCase` truncates every table on teardown and restores
migration data only when `serialized_rollback = True` - which nothing in this suite sets, and
this suite has ~31 `TransactionTestCase`/`transaction=True` tests. So the first run that
includes one destroys the row, and **with `--reuse-db` it never comes back**.

Confirmed by counting the row per database: a freshly-created test DB had 1, the DB reused
across several runs had 0.

This bites the exact workflow `CLAUDE.md` recommends (`--reuse-db` for iterating) while CI on a
fresh database stays green, so it looks like local corruption with no obvious cause.

**Fix**: `core/tests/oauth.py::first_party_application()` - a `get_or_create` writing the same
fields as the migration - now backs the six test modules that need a working first-party client
(`test_e2ee_dual_auth`, `test_external_api_group_controls`, `test_external_api_auth_session`,
`test_external_api_messaging`, `test_external_api_search`, `test_oauth_consent_screen`). Tests
now provide what they need instead of depending on migration state. Against the
*already-poisoned* database this took the same selection from 98 failed / 3 passed to
**1 failed / 189 passed**.

`test_oauth_consent_screen` was missed on the first pass because it never calls
`Application.objects.get` - it just drives the real authorize flow with the real `client_id` and
needs the row to exist. Grepping for the *constant* (`FIRST_PARTY_CLIENT_ID`) rather than for the
query is what finds this class of dependency.

`test_oauth_client_provisioning.py` deliberately still uses `Application.objects.get(...)`:
it asserts what the provisioning command and migration actually wrote, so making it
self-healing would delete the thing it tests. It will still fail on a poisoned database - if
it does, recreate the test DB rather than "fixing" it.

**Not addressed**: the general hazard remains for any *other* migration-seeded reference data.
A suite-wide fix would be `serialized_rollback = True` on the `TransactionTestCase`s (correct
but slow) or moving seed data into fixtures.

## RESOLVED 2026-08-11: `test_pin_suggestion_bulk_partial` reached the real internet

`BulkSuggestionPartialReportingTests::test_accepting_marks_the_suggestions_handled` fails with

```
RuntimeError: External network access is disabled during tests.
Attempted to connect to '208.102.189.146'; mock this integration or use localhost.
```

so an integration on the accept-suggestion path is unmocked and the suite's network guard
(`core/testing_network.py`) catches it. Pre-existing and independent of the OAuth issue above -
it reproduces on a pristine checkout and on a freshly-created database. Per `CLAUDE.md`
("Mock and patch, especially when testing anything that contacts an external service") this
wants the gateway stubbed; worth finding which call it is, since a test that would otherwise
hit a third party on every run is the guard doing its job.

**RESOLVED 2026-08-11.** The unmocked call was `GooglePlaceService._resolve_name`, reached
because accepting a suggestion creates a `Pin` at coordinates with no existing `Location` and
resolves its canonical name inline. Fixed with the patch pair `test_photo_organize` already uses
for the same path (`_resolve_name` + `safely_enqueue_task`); 4 passed, and the full suite is now
green at 10,275 passed.

Tracing it is what surfaced the entry at the top of this file - the *production* behaviour of
making that call synchronously inside the request, up to 200 times in the bulk endpoint - which
remains open.

## RESOLVED 2026-08-12: `bun run test:ts` failed inside happy-dom's event dispatch, only in a full run

**Root cause: the pinned `bun` dependency (see the entry below on `bun run build`).** The suite was
running on the project-local **bun 1.1.6** that `bun run` puts ahead of the real one on `PATH`;
the failure reproduces under 1.1.6 and does not under 1.3.14. After `bun remove bun`:
**383 pass / 0 fail**, three consecutive runs.

Everything below is the investigation that got there, kept because the eliminations are worth
not repeating - and because two of the theories in it were mine and were wrong.


`bun run test:ts` exits 1 with 1-2 failures, always in
`shared/leave-confirmation.test.ts`'s "hrefs that are not navigations" block (most often
"a new-tab link is not challenged", sometimes also "a mixed-case scheme past whitespace").
Like the `bun run build` entry above, this is a CI concern rather than a runtime one.

**It is not an assertion failure.** The thrown error is inside happy-dom itself:

```
TypeError: composedPath[i].dispatchEvent is not a function
  at #goThroughDispatchEventPhases (node_modules/happy-dom/lib/event/EventTarget.js:153)
```

i.e. `event.composedPath()` returned an entry that is no longer an EventTarget while
walking the capture phase.

What was ruled out:
- **Not the test file.** `bun test shared/leave-confirmation.test.ts` alone passes all 26.
  Its `beforeEach` already disarms leftover guards, and that mechanism is documented in
  the file.
- **Not pairwise pollution.** Every other `*.test.ts` was run paired with it individually
  (26 pairs); none reproduces. So it is cumulative across the run, not one bad neighbour.
- **Not fixed by the available patch release.** happy-dom 20.11.1 → 20.11.2 was installed
  and re-run 3x: still fails, and actually became *deterministic* at 2 failures instead of
  flaky at 1-2. Reverted, since it changes the lockfile without fixing anything - but that
  determinism is worth knowing about if someone picks this up, as it makes bisecting easier.
- **Not visible in isolation.** An instrumented probe on the exact failing markup gives a
  clean path: `HTMLAnchorElement | HTMLBodyElement | HTMLHtmlElement | HTMLDocument |
  GlobalWindow`, all with a real `dispatchEvent`.

**The `window.location` hypothesis is disproved** (tested 2026-08-11). Four tests in the file
do reach `leave-confirmation.ts:106`'s `window.location.href = destination`, but a direct
repro shows happy-dom handles that assignment fine and dispatch keeps working afterwards:

```
assignment OK, href now: https://urbanlens.test/elsewhere/
post-navigation dispatch OK
```

So injecting a navigate callback would *not* fix this, and the entry above should not be
read as suggesting it.

What is known:
- Not the test file (passes alone, 26/26).
- Not one bad neighbour: all 26 other `*.test.ts` were run paired with it individually - none
  reproduces.
- **Not either half of the suite either**: splitting the other 26 files in two and running each
  half alongside it reproduces nothing. It needs the *whole* set, which points at a cumulative
  threshold rather than a specific poisoner.
- Every `install()` leaves a capture-phase click listener bound to the shared `document`
  forever (the file documents this and disarms them by flag, but never removes them), and other
  test files bind their own document-level listeners. Across a full run that is a lot of
  accumulated handlers on one `GlobalWindow`, which is the most plausible remaining direction -
  the thrown error is happy-dom walking a `composedPath()` entry that is no longer an
  EventTarget.

**The listener-accumulation hypothesis is also disproved** (tested 2026-08-11).
`installLeaveConfirmation` now returns an `uninstall()` and the test's `beforeEach` calls it, so
no guard's listeners survive its own case. The file still passes 26/26 alone, and the full suite
still fails 1-2 tests in the same block across three consecutive runs. Accumulated listeners from
*this* module are not the cause.

That teardown was kept anyway - the module previously had no way to unbind, and the test file
documented working around it - but it is a testability improvement, **not** a fix for this.

So: two plausible causes tested, both eliminated. What remains is a happy-dom defect triggered by
some cumulative state across the full 27-file run that neither half of the suite reproduces on its
own. Next avenues, in rough order of cost: bisect by *adding* files one at a time to find the
threshold (pairs and halves both come back clean, so it is not a simple poisoner); try a newer
happy-dom than 20.11.2; or run this one file in its own bun process so it stops sharing a
`GlobalWindow` at all, which sidesteps rather than diagnoses.

## ~~LOW 2026-08-11: `check_in`/`cancel_checkin` still write `status` from a possibly-stale instance~~ RESOLVED 2026-08-15

**RESOLVED**: both functions now use the `_resolve_as_found_safe` compare-and-set shape
(`filter(pk=...).exclude(status__in=resolved_statuses()).update(...)`) and return bool; a lost
race returns False with zero side effects (no re-broadcast, no `_conclude_checkin`, no archival
scheduling - the winner already did them), and the external API's mark-safe/cancel endpoints 409
on a lost race instead of silently no-opping. Controller callers ignore the bool by design (their
`is_resolved` pre-checks remain the fast path; a lost race correctly no-ops). 4 new race tests in
`test_safety_resolution_races.py`; 173/173 tests across the six safety-related files pass.
Original entry below.

Found 2026-08-11 while fixing the sweep-driven resolution races (see
`test_safety_resolution_races.py`). `services/visits/safety.py`'s `check_in` and
`cancel_checkin` set `status`/`resolved_at`/`resolved_by_label` with a plain
`save(update_fields=[...])`, unlike `_resolve_as_found_safe`, which does a conditional
UPDATE for exactly this reason.

**Deliberately left alone, and low severity**, because both only ever move a check-in
*into* a terminal state: the worst case is one resolution overwriting another (a contact
reports the owner safe at the same moment the owner checks in), which leaves the status
terminal either way and only gets `resolved_by_label` wrong. Nothing re-selects a
terminal check-in for escalation. Both call sites (`controllers/safety.py:929` and
`external_api/views.py:3144`) additionally pre-check `is_resolved`, so the remaining
window is the milliseconds inside a single request rather than the multi-minute one the
beat sweeps had.

Worth converting to the same conditional-UPDATE shape if this code is touched anyway -
having three of five lifecycle transitions use compare-and-set and two not is the kind of
inconsistency that invites the next person to copy the wrong one.

## RESOLVED 2026-08-05: 38 test failures across search, media-auth, and the REData provider gateways

A sweep over `-k "quota or media or album or photo or storage or upload or relevance or wiki_media
or site_settings or search or redata or dm_search"` went from **38 failed / 1698 passed** to
**1704 passed, 0 failed**. All three causes were the same shape - production code changed
deliberately and its tests were never updated - which is why none of them had an obvious owner.

**1. 36 REData provider tests depended on the test machine's credentials.**
`test_redata_media_gateway.py` and `test_redata_reference_documents_gateway.py` mock the gateway's
`lookup`/`search`, but the providers construct `RedataMediaGateway()` / `RedataReferenceDocumentsGateway()`
themselves, and `RedataGateway.__post_init__` raises `ValueError("UL_REDATA_API_URL must be
configured.")` when the env has no REData credentials. So these passed on a box with credentials
and failed on one without - the exact ambient-state dependency this file documents repeatedly
(see cause 3 in the 2026-07-27 entry, which was the same bug inverted). The shared mixin helpers
now no-op `__post_init__` alongside the mocked call, matching how `test_pin_redata_media_proxy.py`
forces the *unconfigured* state. **Deliberately not fixed by pinning dummy REData settings in
`settings/test.py`**: a dozen `is_configured()` helpers read those values, so configuring them
globally would silently flip behaviour in unrelated tests.

**2. `test_media_auth_mixin.py::test_session_wins_over_a_credential_header` encoded a
since-reversed security decision.** `CredentialOrSessionMediaMixin.resolve_media_profile`'s own
docstring explains at length why a *presented credential now wins over an ambient session*: the
old order let a WebView sharing the site's cookie jar fetch media as whichever account was logged
in, and let a token without `media:read` bypass the scope check entirely whenever a session was
also present. The test still asserted the old order. Rewritten to assert the current behaviour,
plus a new sibling (`test_an_unscoped_credential_cannot_fall_back_to_the_session`) covering the
half with actual teeth. The throttle concern the old test's docstring cited is already covered by
`test_throttle_is_not_charged_to_session_requests`, which sends no header at all.

**3. Two search tests predated deliberate narrowing of the query parser.**
- `test_global_search_engine.py::test_finds_photo_by_generated_keyword` searched `"staircase
  photos"`. `_extract_type_keywords` had been restricted to the query's *first* word (to stop
  "please visit my page" becoming a visits-only search), which also killed the equally natural
  trailing form. **This one was a real product gap, not a stale test** - it now matches a type
  keyword at either end of the query, which restores "abandoned mill photos" while leaving
  mid-sentence matches alone. A trailing keyword that turns out to be part of a real name
  ("Road Trip") is recovered by the engine's existing zero-result fallback, which retries with
  inferred types cleared.
- `test_dm_search.py::test_date_range_phrase_filters_by_created` searched `"reunion 2024"`. The
  parser deliberately requires a preposition before a bare year, because a 4-digit token appears
  in ordinary names ("Building 2024", "Route 2027") - the pattern carries a comment saying so.
  The test now uses `"reunion in 2024"`, with a new sibling asserting the bare form stays a plain
  text search so that narrowness doesn't silently regress.

## RESOLVED 2026-08-12: `bun run build` and the TS suite both failed because `bun` was pinned as a dependency

**Single root cause for two separate entries in this file.** `package.json` declared
`"bun": "^1.0.15"` under **dependencies**, so `bun install` placed a project-local **bun 1.1.6**
at `node_modules/.bin/bun`. `bun run <script>` prepends `node_modules/.bin` to `PATH`, so every
script silently executed on that 1.1.6 instead of the Bun the developer (or the container)
actually has - 1.3.14 in both cases here. `bun` is never imported as a module anywhere; the
dependency did nothing but shadow the real runtime. (`bun-types` in devDependencies is the
legitimate types package and stays.)

Two consequences, both previously filed here as separate bugs with wrong diagnoses:

1. **`--format iife` "not implemented"** - it *is* implemented in 1.3.14; only 1.1.6 rejects it.
   Verified directly: the unmodified build script succeeds under 1.3.14 and fails under 1.1.6.
   The earlier entry blamed 1.3.14, which was wrong.
2. **The `leave-confirmation` test failure** - reproduces under 1.1.6, does not under 1.3.14.
   That entry's happy-dom theories were all chasing a version difference.

**Fix**: `bun remove bun`. `bun run test:ts` then goes from 1-2 failures to **383 pass / 0 fail,
three runs running**, and `bun run build` succeeds with Bun's own `--format iife`.

The chunk-12 workaround (emit `esm`, wrap each classic bundle in an IIFE by hand) was reverted -
it was compensating for the obsolete Bun, and Bun now emits `(() => { ... })()` itself. Verified
after reverting: all four classic bundles build, `node --check` parses each as a *classic script*,
and `window.autosaveGuard`/`confirmDialog`, `UrbanLensE2EE`, `UrbanLensPermissions`,
`UrbanLensWebAuthn` are all still assigned.

**How to notice this class of problem**: `bun run <script>` and the same command typed directly
can run different binaries. If a script behaves differently from the command it contains, compare
`bun run zz --version`-style output against your shell's.

### Original report (diagnosis superseded)


**Root cause**: `bin/build-frontend.ts` builds two groups. The `entries/` group asks for
`--format esm` and succeeds; the `entries-classic/` group asked for **`--format iife`**, and
Bun 1.3.14's bundler implements only `esm`. It raises *after* bundling, which is why the log
shows every chunk built and then a bare error with exit 1, and why the previously-committed
static files stayed in place (nothing was written for that group).

`iife` was the right intent, not an accident: those four bundles are loaded by plain
`<script src>` with no `type="module"`, and `settings/index.html` loads two of them on the same
page - two ESM-shaped bundles sharing one realm collide as soon as both declare the same
top-level `const`.

**Fix**: emit `esm` (the only implemented format) and wrap each classic output in
`(function(){ ... })();` after the build. Verified safe for these four specifically before
doing it - none has a top-level `export` (a syntax error in a classic script) or a top-level
function declaration (which would stop being global); all four expose their API by explicit
`window.X = ...`, which still works from inside a wrapper. The build also now fails loudly if a
future classic entry does introduce a top-level export, rather than emitting a file the browser
cannot parse.

Verified: `bun run build` exits 0 and writes all four bundles; `node --check` parses each as a
classic script (i.e. no ESM syntax survived); `window.autosaveGuard`/`confirmDialog`,
`UrbanLensE2EE`, `UrbanLensPermissions` and `UrbanLensWebAuthn` are all still assigned in the
output. `bun run typecheck` clean. The built files are not git-tracked, so this produces no diff
of its own.

Revisit if Bun implements `--format iife`: the wrapper can then go away.

### Original report



Found while verifying a photo-thumbnail zoom-scaling fix in `map-annotations.ts`. `bun run build`
bundles every entry successfully, then errors out on that message and exits 1 without writing the
final static output for at least `achievements.js`/`article-wysiwyg.js`/etc. (the earlier committed
static files stay in place from the last successful build, so the app itself isn't visibly broken -
this is a fresh-build/CI concern, not a runtime one). Confirmed pre-existing and unrelated to any
in-progress change by `git stash`-ing all working-tree edits and re-running: identical failure on
a clean `@release/v_0_7_0` checkout. Not yet root-caused - worth checking whether one of the entry
points (or a plugin in `bin/build-frontend.ts`) requests a non-ESM output format that this Bun
version's bundler no longer supports. `bun run typecheck` and `bun run test:ts` are unaffected and
both still work normally.

## RESOLVED (already fixed in 36972797; entry was stale as of 2026-08-11): `delete_low_engagement_wikis` deleted *every* wiki

**This is no longer true and was left standing here after the fix.** Verified 2026-08-11: the
filter is live at `delete_low_engagement_wikis.py:91`
(`.filter(Q(pin_owner_count__lte=MIN_PIN_OWNERS) | Q(user_edit_count=0))`, the constant having
been renamed `MAX_PIN_OWNERS` → `MIN_PIN_OWNERS`), and the two tests this entry cited as failing
now pass - the whole `-k low_engagement` selection is 11 passed. `git log -S pin_owner_count__lte`
puts the fix in **36972797** ("Gate official property-owner data; fix 15 pre-existing test
failures", 2026-08-05), the same commit that fixed the tests.

Left in place rather than deleted because a standing "this command destroys all community
content" warning is worth an explicit retraction - anyone who read it before should be able to
find out it was addressed. The original report follows.

### Original report



`management/commands/delete_low_engagement_wikis.py:62` is a commented-out line:

```python
#.filter(Q(pin_owner_count__lte=MAX_PIN_OWNERS) | Q(user_edit_count=0))
```

so the queryset it builds is every `Wiki` in the database. With `--yes` the command deletes all
of them (cascading to child wikis, edits, and related records). The dry-run report still prints
each wiki's real `pin_owners`/`user_edits` counts, so the output *looks* like it selected
correctly - a wiki with `pin_owners=3 user_edits=1` is listed and then deleted.

Committed that way (not a working-tree edit - `git show HEAD` confirms), so it has been live for
a while. Two tests already encode the intended behaviour and currently fail because of it:
`test_delete_low_engagement_wikis.py::DeleteLowEngagementWikisTests::test_no_matches_reports_and_deletes_nothing`
and `::test_wiki_kept_with_enough_pin_owners_and_a_user_edit`.

Found 2026-08-04 during the Place refactor; deliberately not fixed there, since a destructive
command's behaviour should not change as a side effect of an unrelated refactor. The fix looks
like uncommenting the line, but someone should confirm it wasn't disabled on purpose first.

## ~~`.badge--muted` is used everywhere but never defined (and it is not the only one)~~ RESOLVED 2026-08-15 (`1e799da9`)

**RESOLVED**: `.badge.badge--muted` (doubled class so its overrides beat `span.badge`'s 0-1-1
specificity) and `.ul-alert`/`.ul-alert--error` are now defined in `_components.scss`, mirroring
`.safety-badge--muted` and the theme-aware `--ul-color-danger-*` tokens respectively; compiled and
grep-verified in `static/dashboard/style.css`. One correction to this entry's later 2026-08-11
update: `.dm-bubble-menu__item` (+`--danger`) was **never** missing - it is defined via SCSS
`&__item` parent-selector nesting inside `.dm-bubble-menu` (`_messages.scss:1649-1668`), which a
literal-selector grep cannot see. When re-enumerating undefined classes, grep the *compiled*
`style.css`, not the SCSS sources. Original entry below for context.

**Update 2026-08-11**: this is a small class of issue, not a one-off. Two more components are
referenced only by templates and defined in *no* stylesheet - not the SCSS, not any inline
`<style>` block, not the compiled CSS, and not set from TypeScript:

- **`.ul-alert` / `.ul-alert--error`** - the error banner on the site-admin cost page
  (`partials/admin/_cost_admin_body.html:14,21`, `<div class="ul-alert ul-alert--error"
  role="alert">`). Neither the base nor the modifier exists, so a *failure* message renders as an
  unstyled div. `role="alert"` still works, so this is visual only.
- **`.dm-bubble-menu__item`** (+ its `--danger` modifier) - the group-chat overflow menu buttons
  in `partials/messages/_group_thread.html`.

Same reason as the original entry for not fixing them here: the missing piece is a colour and
treatment, which is a design decision rather than a bug fix.

**How to re-enumerate** (worth recording, because the naive version of this check is badly
misleading): extract `class="..."` tokens containing `--` from the templates, then subtract
selectors found across *all* style sources. Checking SCSS alone reports ~75 candidates, most of
them false - a good number of components are styled by inline `<style>` blocks in their own
template (`.tools-card--wide` in `pages/tools/index.html`, for example). Against SCSS + inline
`<style>` + compiled CSS the list drops to ~70, and much of the remainder is still noise:
class strings assembled in template expressions (they show up with stray quote characters, e.g.
`.cal-cell--today'`) and semantic hooks that carry no styling by design. Only the ones verified
absent from stylesheets *and* TypeScript, like the two above, are worth acting on.

### Original report



Found 2026-07-31 while building the PinImportFailure review queue. `_pin_suggestion_card.html`,
`_pin_merge_suggestion_card.html`, and `_pin_import_failure_card.html` all render
`<span class="badge badge--muted">...</span>` for their origin/reason badges, but grepping the
SCSS source turns up only a bare `.badge` rule (a right-floated "count badge" style) - no
`.badge--muted` modifier is defined anywhere. These badges have likely been rendering with just
the base `.badge` look (or unstyled, depending on cascade) on every card that uses them since
whichever of those templates shipped first. Pre-dates the import-failures feature; not fixed as
part of it since the badges still render (just not visually "muted"), and fixing it means picking
an actual muted color/treatment, which is a design call rather than a bug fix.

## RESOLVED 2026-07-31: gunicorn's gevent worker + `async_to_sync(channel_layer.group_send)` corrupted unrelated concurrent requests' `SynchronousOnlyOperation` check

Production intermittently 500'd on completely ordinary synchronous ORM calls - the reported
repro was `location.pins.count()` deep in SpotGuessr's difficulty-weighting code
(`services/spotguessr/selection.py::_proxy_difficulty_rating`), several frames from anything
async. Even the 500 handler's own fallback render died the same way (`SiteSettings.get_current()`
in `context_processors.py`), which was the tell that this had nothing to do with SpotGuessr.

**Root cause**: `package.json`'s `start` script runs `gunicorn ... -k gevent` (see also
`gunicorn.conf.py`, which patches psycopg2 for gevent cooperation via psycogreen). Gevent
cooperatively schedules every in-flight request as a "greenlet," but many greenlets share exactly
one real OS thread per worker process (`WEB_CONCURRENCY` workers, each hosting many greenlets).
Django's `SynchronousOnlyOperation` check reads `asyncio.get_running_loop()`, and asyncio's
"is a loop currently running" flag is tracked at the C level **per OS thread**, not per greenlet -
gevent's monkeypatching can virtualize `threading.local` and friends for greenlets, but it cannot
virtualize that.

The codebase calls `asgiref.sync.async_to_sync(channel_layer.group_send)(...)` throughout the
real-time layer (`services/messaging/direct_messages.py`, `services/messaging/group_chats.py`, `services/visits/safety.py`,
`models/notifications/signals.py`, and the game realtime modules
`services/{spotguessr,trivia,consensus}/realtime.py`). Whenever any of those is mid-flight -
specifically while `run_until_complete` is doing network I/O against the Valkey channel-layer
backend, a cooperative yield point for gevent - the worker's shared OS thread is flagged "inside a
running event loop." If gevent's hub switches to a *different*, completely unrelated greenlet
during that window and that greenlet touches the ORM (as virtually every view does), it incorrectly
trips `SynchronousOnlyOperation`. This is systemic: any concurrent chat message, notification, or
game-session broadcast could poison any other in-flight request on the same gevent worker for the
duration of the `group_send` call.

**Fix**: every `async_to_sync(channel_layer.group_send)` call site now goes through
`services.core.channel_broadcast.send_group_message(group, message)`, which enqueues
`tasks.broadcast_channel_group_message` on `celery-worker`'s prefork pool (a real, separate OS
process per slot - confirmed never gevent-patched, per `celery-worker-panels`'s `--pool=threads`
comment and the plain default `celery-worker` command in `docker-compose.yml`) instead of calling
`async_to_sync` inline in the request. That task performs the actual call and swallows/logs
delivery failures, matching every caller's prior "already durably saved, live delivery is a bonus"
contract. Regression coverage: `tests/hypothesis/test_channel_broadcast.py` (the new dispatch
boundary and task), `tests/hypothesis/test_notification_push.py` (updated to assert
`send_group_message` is called rather than mocking the old inline `get_channel_layer`/`group_send`
path).

Deliberately not done: switching the WSGI worker off gevent entirely, or reimplementing
`channels_redis`'s wire protocol with a plain sync Redis client to avoid asyncio altogether - both
were considered (see the session's discussion) but are larger architecture changes; routing through
Celery fixes the corruption at its only real source (asyncio-in-request) with a contained diff and
fits the codebase's existing "Celery for anything that shouldn't block the request thread"
convention. Broadcasts now pay a small broker round-trip instead of running inline - acceptable for
this app's near-real-time (not hard-real-time) chat/notification/game UX.

## ~~2026-07-28: Satellite/street-view imagery render path re-runs the full provider chain even when "ready"~~ RESOLVED (verified 2026-08-15)

**RESOLVED**: the harm named here - unbounded full-chain latency with no fast-placeholder
short-circuit - no longer exists. `controllers/pin.py:1043-1044` now returns a fast polling
placeholder (`_pending_panel` → `schedule_panel_fetch`) whenever `is_ready` is false, including
the lapsed-but-not-rewarmed gap this entry describes; the comment at `pin.py:1040-1042` states the
chain "must never run on the request path". The ready path runs `collect()` against warm
per-provider caches bounded by `call_with_deadline(EXTERNAL_CALL_DEADLINE=20s)`
(`pin.py:1059-1064`), and `SLIDES_READY_TTL_SECONDS=12h` is deliberately shorter than the 24h
slide caches so the marker lapses before entries can expire mid-render
(`external_data.py:918-922`). Residual (accepted trade-off, documented at `pin.py:1046-1049`): a
per-provider cache eviction inside the ready window can still refetch inline, bounded to 20s -
"bounded staleness beats an unbounded inline refetch". Original entry below for context.

`services/pins/external_data.py`'s `SlidesPanelSource` (base of `SatellitePanelSource`/street-view
equivalent) tracks readiness with a summary marker (`is_ready`, `ready_key`) set after a background
warm-up pass, separate from each provider's own 24h slide cache (`SLIDES_READY_TTL_SECONDS`,
line ~108). The class docstring (line ~875) confirms "the Celery warm-up task and the request-path
render share this exact function" - `collect()` runs the same per-provider gateway chain
(`collect_satellite_slides`/`collect_street_view_slides`) on the request thread regardless of
whether `is_ready` is true, rather than reading a fully-materialized result. In practice this is
usually cheap (each provider serves from its own warm cache), but there's no short-circuit: a
request landing in the gap where the summary marker has lapsed but not yet re-warmed pays the full
provider-chain latency inline on the request thread instead of getting a fast placeholder.

Fix would be to have `render`/`api_payload` read a materialized slide list when `is_ready` is true
instead of re-invoking `collect()`, reserving the shared function for the warm-up task alone. Not
investigated further - flagged while auditing `docs/notes/mobile_app_notes.md`'s claim (D8) that
this was already logged here, which it wasn't until now.

## RESOLVED 2026-07-28 (documented judgement call): restoring legacy `status='Muted'` friendships

Migration `0020_friendship_muted_flag` has to guess what a `status='Muted'` row was *before* it
was muted, because the old encoding overwrote the previous status and stored it nowhere. It
restores those rows to `Accepted` with `muted=True`. That is a judgement call and is recorded
here so a future audit does not have to re-derive it:

- The only user-reachable mute path is `FriendController.mute_friend` ->
  `services.social.friendship.mute_profile`, and the only template rendering that URL
  (`partials/profile/_profile_hero_body.html`) emits the Mute button **exclusively** inside its
  `friendship_status == 'accepted'` branch. So every mute a real user performed started from
  `Accepted`, which makes the restore faithful rather than a widening of access.
- `Friendship.mute()` (a classmethod that created a `Muted` row for two strangers) could have
  produced non-accepted rows, but `git log -S "Friendship.mute"` over
  `controllers/`, `services/` and `external_api/` returns nothing across the whole history - it
  was only ever called by its own unit test. It is deleted in this change.
- The external API's `FriendMuteView` *can* mute a non-accepted row, but it exists only on the
  unreleased `feature/external-api-mobile-v2` branch that introduces this migration, so no
  deployed database can hold a row it wrote.

If any of those three assumptions turns out to be false for a given deployment, the affected
rows are ones where two profiles are now treated as accepted friends when they previously were
not. Auditing that is a single query: `Friendship.objects.filter(muted=True, status='Accepted')`
with `created`/`updated` predating the deploy of `0020`.

## FIXED 2026-07-28: Google Calendar export leaked trip-mates' hidden coordinates

**Severity: privacy, cross-user, and irreversible once it fired** - the data went to a third
party (Google), where no later UrbanLens privacy change can reach it.

`services/trips/calendar_sync.py::_activity_location_string` honoured only `TripActivity.location_hidden`
and ignored the adder's `Profile.trip_pin_location_visibility` gate that every other trip surface
applies via `services/trips/trip_visibility.py::viewer_hidden_activity_ids` (the activities panel, the
trip map, AI trip suggestions). A trip is a shared space, so exporting one wrote **other members'**
coordinates - precisely the ones the trip screen deliberately withholds from the exporter - into
the exporter's Google Calendar, as the `location` field of both the all-day trip event
(`trip_to_event_body` -> `_trip_location_string`) and the per-activity timed events
(`activity_to_event_body`).

Repro (pre-fix): two profiles on one trip; the adder sets `trip_pin_location_visibility = no_one`
and adds an activity with a `Location`; the other member exports the trip
(`POST /dashboard/trips/<slug>/calendar/export/`, or the external API's
`POST /trips/<slug>/calendar/`, or any auto-sync push via `push_auto_synced_trip_changes`) ->
the event body carries the address. The trip screen shows that same member no coordinates at all.

Fixed by making `export_trip_to_calendar` compute the viewer's hidden-activity set once
(`_hidden_activity_ids_for`, which runs the shared `viewer_hidden_activity_ids` for
`account.profile`) and thread it through `trip_to_event_body`, `_trip_location_string`,
`_sync_activity_events` and `activity_to_event_body`. A hidden activity still gets its event -
the exporter is committed to be somewhere and a gap in their calendar would be its own bug -
just without a `location`. Regression coverage:
`tests/hypothesis/test_external_api_trip_calendar.py::ExportRespectsAdderVisibilityTests`.

Left open deliberately: the pure-mapping helpers still accept `hidden_activity_ids=None`
("no viewer gate"), which is correct for the property tests that call them with unsaved trips
but means a *new* caller that forgets to pass it reintroduces the leak. Worth revisiting as a
required argument once no caller needs the viewerless form.

## RESOLVED 2026-07-27: `test_websocket_auth.py::test_valid_api_key_authenticates_an_anonymous_socket` times out

Was `asyncio.CancelledError -> TimeoutError` (asgiref/timeout.py:108), and was **confirmed
pre-existing** by stashing the Place-consolidation change set (identical `1 failed, 6 passed`
both ways).

The asymmetry that made it look consumer-specific - the structurally identical OAuth2 sibling
passed - was the actual clue, just not in the direction first guessed. It was not a
`database_sync_to_async` deadlock. Nothing in the project overrode `PASSWORD_HASHERS`, so tests
ran Django's default PBKDF2 at ~1.2M iterations; `authenticate_api_key`'s `check_password` ran
*inside the WebSocket handshake* and blew past `WebsocketCommunicator.connect()`'s 1-second
default timeout. The OAuth2 path is a plain indexed token lookup with no hashing, so it never
came close.

Fixed by setting `PASSWORD_HASHERS = ["...MD5PasswordHasher"]` in `settings/test.py` (test-only;
base.py keeps the real hashers everywhere else). Speeds up every test that bakes a User as a
side benefit.

## RESOLVED 2026-07-27: `get_nearby_or_create(threshold_meters=0)` could 500 on sub-precision coordinate collisions

`Location.latitude`/`longitude` are `DecimalField(max_digits=9, decimal_places=6)`, so the
database rounds to 6dp on insert - but `Location.save()` builds the PostGIS `point` from the raw
unrounded float (`models/location/model.py:426-429`). Two coordinates that differ only below 6dp
therefore round to the *same* stored (latitude, longitude) while their stored points sit ~1cm
apart.

With `threshold_meters=0` (`models/location/queryset.py:117-161`) that combination is
unreachable-by-lookup but blocked-on-insert: the `point__distance_lte=(point, D(m=0))` probe
misses the existing row, the insert then trips the `(latitude, longitude)` unique constraint, and
the `IntegrityError` handler re-runs the *same* zero-distance probe, misses again, and re-raises -
surfacing as a 500.

Repro: call it twice with e.g. `42.00000014` then `42.00000006` (same longitude).

Fixed by `Location.objects.get_exact_or_create` (`models/location/queryset.py`), which matches on
the stored coordinates - what the unique constraint actually enforces - rather than a
zero-distance geometry probe. Every exact-coordinate caller now goes through it:
`pin_creation.resolve_child_pin_location`, `pin_edit.move_pin_to_coordinates`, and
`detail_pins._location_for_child_wiki`.

Two adjacent bugs surfaced while fixing it, both also fixed:

- `_location_for_child_wiki` handled "a wiki already owns this Location" by inserting a **second
  Location at the same coordinates**, which the `(latitude, longitude)` unique constraint refuses
  outright - a guaranteed 500 whenever a user dropped a child wiki marker on a point that already
  had one. It now raises `ChildWikiLocationError`, surfaced as a 400 ("place it slightly apart"),
  matching the child-pin rule. The child-wiki *move* path excludes the wiki being moved, so a
  stay-put drag is still a no-op.
- `move_pin_to_coordinates` let a root pin move onto a Location where the owner already had
  another root pin, which violates `db_pin_unique_location_per_profile` and surfaced as an
  unhandled `IntegrityError`. It now raises `PinMoveError` (400 on both the internal and external
  endpoints). Child pins are deliberately unaffected - sharing a parcel is their purpose.

## RESOLVED 2026-07-27: assigning `Location.cid` performed a synchronous Google lookup

`Location.cid`'s setter (`models/location/model.py`) called
`GooglePlaceService().set_cid_for_entity(self, value)` and took that method's
`fetch_if_missing=True` default, so `location.cid = 123` - an assignment that reads like setting a
field - issued a live Google call to resolve a place name for the coordinates. This is what made
`test_legacy_cid_coordinate_fix.py` hit the network (see cause 1 in the big entry above); the test
was fixed at the time, the setter was not.

The setter now passes `fetch_if_missing=False`, matching `place_name`'s documented cache-only
stance a few lines above it. Callers that genuinely want the lookup should call the service
directly, where the cost is visible.

Worth knowing: **every** production caller of `set_cid_for_entity` (`services/apis/locations/
google/maps.py:240,769`) already passed `fetch_if_missing=False` explicitly. The setter was the
only code path anywhere taking the blocking default, so that default currently has no users. It
is left as-is - flipping it is a wider API decision - but a future caller relying on it should
know it is a trap rather than a considered default.

## RESOLVED 2026-07-27: nine pre-existing friend-invite / pin-sync test failures on `feature/external-api-mobile-v2`

**Resolved.** The open question below - "is the gate right, or do the tests encode a real product
requirement?" - was settled in favour of the gate, on three pieces of evidence: the code comment
documents it as a deliberate fix, `request_friend` runs the same evaluator (so exempting the email
path would reintroduce exactly the asymmetry the fix closed), and the bypass it replaced is
recorded as a vulnerability further down this file. Knowing someone's email address is not a
secret worth overriding their stated preference for.

So the eight friend-invite tests were stale. They now use a `make_invitable_user` helper
(`test_friend_invite_privacy.py`) that opts the *target* into `ANYONE`, keeping each test on its
actual subject; `test_response_identical_regardless_of_target_friend_request_visibility` sets both
ends explicitly since it is the one test genuinely about the gate. **29 passed.**

The ninth, `PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid`, was fixed
independently by the child-pin location work (`resolve_child_pin_location` /
`get_exact_or_create`) - its `PinCreationError: You already have a pin at this location.` was that
exact bug. **10 passed.**

**RESOLVED 2026-07-28**: the "invite a friend by email is a no-op for two already-registered
users" consequence flagged above was decided in favour of option (b) - soften the default.
`friend_request_visibility`'s default is now `ANYONE` rather than `ANYTHING_IN_COMMON`
(`models/profile/model.py`, migration `0018_alter_friend_request_visibility_default.py`), on the
reasoning that having an account should never make a user *harder* to reach by friend request than
not having one - which is what the stricter default did, since `invite_by_email`'s
unregistered-address branch always sends the invitation unconditionally. The migration backfills
existing profiles still at the old default (not ones a user deliberately changed) - see the
migration's own comment for the reasoning, mirrored from the `welcome_onboarding_complete`
precedent in `0002`/`0003`. `test_anything_in_common.py::VisibilityDefaultsTests` updated to match
(every other `ANYTHING_IN_COMMON`-by-default field is unaffected - this was scoped to
`friend_request_visibility` only). 205 passed across every suite touching this setting.

## RESOLVED 2026-08-12: WhatsApp/SMS alerts never fire for safety check-in partner invites

`services/notifications/notification_text_alerts.py:114-115` derives the preference column name from the
notification's own type:

```python
prefix = notification.notification_type
return bool(getattr(prefs, f"{prefix}_whatsapp", False)), bool(getattr(prefs, f"{prefix}_sms", False))
```

That works for 11 of the 12 preference stems, but **not** for the safety-check-in partner
invite. `NotificationType.SAFETY_CHECKIN_PARTNER_INVITE` has the value
`"safety_ci_partner_invite"` (`models/notifications/meta/type.py:26`), while the
`NotificationPreference` columns are named `safety_checkin_partner_invite`,
`safety_checkin_partner_invite_whatsapp`, `safety_checkin_partner_invite_sms`
(`models/notifications/model.py:180-182`). The lookup therefore misses, and the
`getattr(..., False)` default silently reports "user does not want text alerts" - so a user
who explicitly enabled WhatsApp/SMS for partner invites never receives them, with no error
anywhere.

Note the same mismatch does *not* affect `wiki_safety_checkin`, whose type value and column
name do agree.

Fix is a rename on one side plus a migration (and a check for any other consumer deriving
field names from type values). Deliberately not done as a drive-by during the external-API
social/notifications build, since it changes either a stored enum value or three column names.

Guarded meanwhile by
`tests/hypothesis/test_external_api_notifications.py::NotificationPreferenceCoverageTests::test_one_preference_stem_does_not_match_its_notification_type`,
which asserts the mismatch explicitly so that fixing it fails loudly rather than silently
changing the external API's preference field names.

**Resolved 2026-08-12, without the rename or the migration.** This entry assumed the fix had to be
"a rename on one side plus a migration", which is what kept it open for two weeks. It doesn't: the
column stem is the enum *member name* in every other consumer, so `_enabled_channels` now derives
it the same way (`NotificationType(value).name.lower()`) instead of from the value. That fixes the
one divergent type and is a no-op for the other 31 - measured: 12 types resolved by value, 13 by
member name, one difference.

A second defect had to be fixed with it, or the first would have stayed invisible:
`TEXT_ALERTABLE_TYPES` omitted the type entirely, so the lookup was never reached. Its own
docstring defines membership as "types with a toggle pair", MESSAGE excepted - and the partner
invite has a full pair, persisted and settable via the external API. 13 stems have a pair, 11 were
listed, and the two omissions were `message` (deliberate) and this one (not).

The stem/value mismatch itself is untouched, so the external API's field names are unchanged and
the guard test above still holds. New: `test_text_alert_preference_stems.py` asserts every stem
with a toggle pair is alertable, so a settable-but-unfirable toggle cannot be introduced again.
`test_notification_text_alerts.py::test_every_alertable_type_has_both_preference_fields` was
updated to resolve by member name too - it derived columns from the value, which only held while
the broken type was absent from the set.

## RESOLVED 2026-08-12: notification "friend accepted" loses its source_profile on one path

`services/social/friendship.py::accept_friend_request` (ported verbatim from the old
`FriendController.accept_friend`) creates the `FRIEND_ACCEPTED` notification **without**
`source_profile`, whereas `request_or_accept_friendship` and
`FriendController.friend_request_respond` both set it. The external API's
`NotificationSerializer` exposes `source_profile`, so a mobile client sees a null actor for
notifications produced by that one path and cannot link back to the profile. Left as-is
during the extraction to keep the refactor behaviour-preserving; setting
`source_profile=actor` there is almost certainly correct but should be done with a test that
pins the intended behaviour on all three paths.

**Resolved 2026-08-12.** `source_profile=actor` set on `accept_friend_request`, with
`test_friend_accepted_source_profile.py` pinning all three paths as this entry asked. One test
goes further than "not null" and asserts the named actor agrees with the message text and url in
the same row - those already referred to that profile, which is what made the omission a
contradiction rather than just a gap. A static completeness check fails if a fourth site ever
raises `FRIEND_ACCEPTED` without it.

**Status as of 2026-07-23 (cleanup)**: all fully-resolved entries have been removed from this
file - resolution details live in git history (this file's prior revisions) and
`docs/notes/ai/completed.md`. Recently closed, for orientation: the whole PR #111 cluster
(CodeQL triage, both SSRFs, E2EE password-policy endpoint, opaque rotation member IDs,
per-recipient WebSocket payloads, media-proxy URL signing), the WhatsApp/SMS delivery wiring
for every notification toggle, trip-comment `comment_visibility` gating, campus-aware
Wikipedia search (UL-354), the Overpass pool overhaul (UL-355 + self-hosted primary +
empty-result cross-validation), Internet Archive `texts` tiles, the child-pin terminology
sweep, the compose test "pod" for DB-backed tests, and `schedule_panel_fetch`'s broker-outage
handling.

**Closed in the post-cleanup round, same day**: **PinTombstone pruning** (daily
`prune_pin_tombstones` beat task, 400-day retention in
`services.pins.pin_sync.TOMBSTONE_RETENTION`; `pins/deleted/` now returns **410 Gone +
`full_resync_required`** when `deleted_since` predates the retention floor, so pruning can
never cause a silent miss - 3 new tests in `test_external_api.py`), and the **four export
importers** (see the struck entry below for the design decisions that shaped them - 24 new
round-trip tests in `test_export_import_completeness.py`). Everything below is genuinely
still open.

**Feature build, 2026-07-24** (from the ROADMAP.md feature analysis, five of the six
recommended items - see ROADMAP.md for full RESOLVED notes and commit hashes): public pins
by community vote (UL-58), trip-planning OSRM drive-time legs + optional/generated trip
names (UL-60 partial, UL-360), an AI chat assistant with an allowlisted tool loop (UL-293),
KML/GPX/GeoJSON/CSV quick exports + emailed full exports (UL-382, UL-373), and
recency-weighted boundary voting (Pin Restructure section). All five pod-tested green
(60 + 30 = 90 new tests) and browser-verified on dev.urbanlens.org. Offline maps (UL-287,
the sixth recommended item) was intentionally skipped this round. Explicitly **not** built:
UL-377's search/list-scoped targeted exports (blocked on lists, which don't exist yet),
UL-60's AI-driven schedule-timing suggestions and inline "AI suggests pins for this trip" UI
(the assistant can add a specific pin to a trip on request, which covers part of this in a
chat-driven form only), and UL-163's broader AI-sandboxing ticket (MCP security, local
models) - the assistant's allowlist-only tool loop is a first answer to the same concern but
doesn't close that ticket. The boundary-voting dialog auto-opens only while zero votes exist
(not, as the spec's prose could be read, until consensus forms) - a deliberate simplification
worth knowing about if the UX is revisited.

**RESOLVED 2026-07-25**: the `TripMembership.rsvp` choices drift noted above is now migrated -
`0029_alter_tripmembership_rsvp.py` carries the `AlterField` for the `"Going"`/`"Not Coming"`/
`"Maybe"` labels. This checkout briefly had two different, unrelated `0027_*` migrations as
sibling leaves off `0025` (the indoor_outdoor/rsvp work here, and `0027_safety_checkin_partners.py`
from a separate concurrent session); since nothing had been pushed anywhere migration state is
persisted, this was resolved by resequencing instead of a merge migration - the indoor_outdoor
migration was renumbered to `0028` and now depends on `0027_safety_checkin_partners`, with the
rsvp `AlterField` as `0029` after it. Single linear chain, no merge migration needed.

---

## ~~Verification debt~~ RESOLVED 2026-07-23 (pod ran; all session-added tests pass) → 17 PRE-EXISTING full-suite failures triaged below

**The debt itself is cleared**: the test pod ran for the first time (it works - two workflow
gotchas found and documented in CLAUDE.md: the runner bakes source at build time, and
rebuilding it orphans test-db/test-valkey's shared namespace). The 2026-07-23 rounds' own
test files were executed and now **all pass** - the run surfaced 14 findings (2 real code
bugs in that day's work: the photo-proxy signature was computed over the raw name while
Django delivers the percent-encoded path segment, and same-instance comment re-imports
duplicated once the uuid was taken; plus 6 stale/fragile pre-existing tests) - all fixed in
`06de47fd`/`35ac4100`.

**The FULL suite then ran end-to-end for the first time ever: 6,277 passed, 17 failed
(34m45s).** None of the 17 touch code changed on 2026-07-23; they are pre-existing test debt
that had simply never executed against a real DB. Triage (each verified from the run log,
`/tmp/pod-full.log` on chiron):

- **`test_site_admin_stats` (4) + `test_infrastructure_stats` (1)** - the stats collectors
  probe the real infra services and trip `LocalhostOnlyNetwork` on the dev stack's
  container-bridge IPs (`172.18.0.10`). These tests need the probes mocked (per the repo's
  own testing policy) - they can never pass inside the pod as written.
- **`test_avatar_colors::GroupMemberSearchAvatarColorTests`** - `0 != 4`: member search now
  filters through `can_view_profile`, and the test's baker profiles keep the default
  `profile_visibility` (ANYTHING_IN_COMMON) with nothing in common → 0 results. Stale since
  the member-search masking hardening; fix by setting candidates' visibility (mirror
  `_profile()` in test_identity_visibility.py).
- **`test_flickr_album_import::test_blank_url_shows_an_error`** - the pod has no Flickr
  keys, so the view short-circuits to "Flickr integration is not configured" before the
  blank-URL branch; the test must stub the settings keys.
- **`test_media_own_photos_preview` (2)** - endpoint returns 204 where the tests expect
  200-with-tiles; mechanism not yet dug into (likely fixture gap - files/coords - or a
  moved gate).
- **`test_pin_edit_controller::PinDescriptionEditableTests` (2)** - the rendered page no
  longer carries `data-raw-description=""` / carries `pin-description--empty` unexpectedly;
  description-editor markup drift.
- **`test_profile_hero_meta_editable` (2)** - "Add your area..." placeholders NOW render
  where the tests expect them hidden; either deliberate own-profile placeholder behavior
  change (update tests) or a regression in the hidden-when-empty rule (check intent first).
- **`test_settings_tos_accepted_display`** - "Mar 4, 2025" not found though the label
  renders; date-format drift.
- **`test_pin_media_endpoints::test_media_relevance_route_reaches_the_post_handler`** -
  `TypeError: Cannot mix str and non-str arguments` (an os.path/reverse join receiving a
  Mock/None); needs its traceback read.
- **`test_property_records_plugin`** - `test_the_locations_address_is_passed_through_as_the_situs_search_key`
  assigns `location.address`, which is now a read-only property (`AttributeError: no setter`).

**Suggested next step**: one focused session over these 9 files - none looks like a
production bug on its face (env coupling, fixture rot, template drift), but
`test_media_own_photos_preview`'s 204 and `test_profile_hero_meta_editable`'s
placeholder-visibility change deserve a real look at intent before the tests are edited to
match current behavior. The pod is left running on chiron for it.

---

## ~~UL-255: "Remember last map position"~~ (RESOLVED 2026-07-23 - browser-verified WORKING, recommend closing)

**RESOLVED 2026-07-23**: reproduced the exact suspect scenario in a real browser against dev
(Playwright, REMEMBER mode enabled, remembered position cleared first): two real mouse-drag
pans fired 2 debounced POSTs to `settings/map-position/` with the panned coordinates, and a
**fresh navigation to the bare map URL** (no `?lat/lng/zoom`) restored the view to the exact
remembered position - delta 0.00000°/0.00000°, zoom matched. The REMEMBER chain works
end-to-end on fresh navigation, and Jess confirmed the other scenario (same-tab reload where
URL params win) is intended behavior. Both possible readings of the original report are
therefore accounted for; recommend closing UL-255. If it recurs, capture the exact
navigation path - the repro script is `ul255.js` in this session's scratchpad pattern
(login → pan → fresh goto → compare `map.getCenter()`).

---

## ~~Saved-filter include/exclude label picker: no drag-reorder or formula mode~~ (RESOLVED 2026-07-23, browser-verified on dev)

**RESOLVED 2026-07-23** - the authorized extraction is done and verified live:

- **`frontend/ts/shared/label-picker.ts`** (installed globally as
  `window.UrbanLensLabelPicker` by core.js) now owns both picker shapes:
  `createFilterPicker` (the map sidebar's full engine - include/exclude columns, chip
  dragging, AND/OR combinator, formula bar with tokenizer/parser/suggestions,
  `label_groups` serialization) and `createChipPicker` (the flat search+chips component the
  bulk-edit dialog and saved-filter scripts each used to duplicate). One deliberate
  improvement over the inline original: label names are HTML-escaped in generated
  chip/suggestion markup (the old code interpolated them raw - a UL-362-class XSS vector).
- **Main map**: the ~650-line inline engine is gone; the page instantiates the module
  against the existing fp-* DOM (inline on* handlers removed - the module wires delegated
  listeners, which also covers labels appended later by the create-label dialog).
  `applySavedFilter` merges via `mergeIncludeIds`, reset via `clear()`.
- **Bulk-edit dialog**: `_makeLabelChipPicker` is a thin id-based wrapper over
  `createChipPicker`. The rich include/exclude pairing deliberately does NOT apply there -
  add-labels and remove-labels are separate actions with separate candidate pools.
- **Saved-filter dialog + detail page**: the two flat pickers became ONE rich picker
  (`_saved_filter_label_picker.html`, sf-* ids, reusing the global fp-* styles). It
  serializes structured `label_groups` into the form (the create/edit endpoints already
  parsed that field) AND mirrors flat `tags`/`exclude_tags` hidden checkboxes; it seeds
  from stored groups (falling back to flat sets), so formulas round-trip and the "advanced
  rules will be replaced" warning was removed as no longer true.

**Browser-verified on dev.urbanlens.org** (Playwright in the official image on the chiron
VM, driving a real login): 22/22 checks - click-include, right-click-exclude, AND/OR
toggle, chip drag include→exclude, chip-click removal, formula `(Visited / Rooftop) -
Demolished` parsing to `[{or,[..]},{not,[..]}]`, filter POSTs firing, and on the
saved-filter page: seeding from flat criteria, hidden-input sync, formula entry, save, and
byte-identical `label_groups` round-trip after reload (map preview showed exactly the 2
matching pins). Screenshots reviewed. Remaining follow-up: the two updated template tests
(`test_saved_filter_detail.py`, `test_region_filter.py`) run in the compose test pod with
the rest of the verification-debt list.

---

## ~~Data export: comments/photos/trips/direct_messages have no importer~~ (RESOLVED 2026-07-23)

**RESOLVED 2026-07-23** - all four built (`_import_comments`/`_import_photos`/`_import_trips`/
`_import_direct_messages` in `services/import_export/import_data.py`, wired into `_IMPORT_ORDER` between
visit_history and connections). Export shape fixed first: `_resolve_target` now emits a
`target_uuid` (pin or wiki uuid; names are matched never), photos metadata gained
`media_type`, trips gained `is_creator` + `member_uuids`, and DM rows gained `partner_uuid`
(withheld whenever the partner's identity is masked from the exporter). Design decisions,
recorded because they're deliberately narrower than "import everything":

- **Comments**: uuid-idempotent; pin targets must resolve to the importer's OWN pin (via
  `pin_uuid_map` or direct lookup) and wiki targets must pass `location_visible_to` - a
  user-supplied archive can neither attach content to someone else's pin nor to a wiki its
  owner can't see. Unresolvable targets skip with a warning (an orphan comment renders
  nowhere). Exported `created` timestamps are preserved via post-create `update()`.
- **Photos**: files re-enter storage through the same `file_size_error_for_upload` /
  `quota_error_for_upload` checks as a fresh upload (archive contents were already
  malware-scanned at extraction); metadata filenames are `basename()`-neutralized against
  traversal; unresolvable targets still import as unattached uploads (the file is the user's
  own data regardless); labels reattach via `label_uuid_map`.
- **Trips**: requests-not-facts, mirroring `_import_connections` - only trips the user
  *created* are rebuilt (`is_creator`), an existing uuid is never claimed, and exported
  members are re-invited only when they're the importer's current connections, as
  `STATUS_INVITED` (with the standard added-to-trip notification), capped by
  `max_trip_members` / the upcoming-trips limit.
- **Direct messages**: only the user's own SENT PLAINTEXT rows are restored - received rows
  would let a crafted archive fabricate messages "from" a real user, and encrypted rows are
  sealed to the exporting account's key material the server can't re-wrap (the ciphertext
  stays readable in the archive itself; decision adjusted from "import ciphertext rows"
  during implementation for exactly that reason). Restores require the partner to exist,
  `can_direct_message` to still permit, and no mute either way; rows are inserted directly
  (never through `create_direct_message`) so restoring history pushes no live events, bell
  notifications, or text alerts at the partner; exported read state and timestamps are
  preserved so nothing lands as new/unread.

24 round-trip tests in `test_export_import_completeness.py` (DB-backed - see the
verification-debt entry above).

---

## ~~Hardcoded (non-theme-aware) `#2563eb`/`#4f46e5` blue in `_explainer.scss`, `_map.scss`, `_e2ee.scss`~~ (RESOLVED 2026-07-23 - browser-verified acceptable in both themes, no change needed)

**RESOLVED 2026-07-23**: the browser verification the entry was waiting for happened - the
components were rendered in BOTH themes (real login on dev; the explainer/toggle/E2EE-button
composite via an injected exact-markup probe, plus the map onboarding card observed live
during the label-picker verification) and none is a legibility bug:

- **Explainer** (`.ul-page-explainer` + the (?) toggle): legible in light and dark; the blue
  "TIP" kicker on the dark glass panel is the lowest-contrast piece but reads clearly (bold,
  uppercase, short) - deliberate branding, not breakage.
- **Map onboarding card** (`_map.scss` gradient icon + `FAST START` eyebrow): verified live
  in dark mode during the picker work - legible.
- **E2EE** (`#4f46e5`): a solid indigo button with white text (theme-independent by
  construction) and a title-icon accent - fine in both themes.

Per Jess's decision these were left untouched; converting them to `--ul-primary-color`
tokens remains optional polish, not a defect. Screenshot evidence: `blues-probe-dark.png` /
`blues-probe-light.png` from this session's verification run.

---

## ~~SpotGuessr: down-voted photos permanently shrink a small pin pool's playable rounds, with no expiry~~ RESOLVED 2026-08-15 (the expiry half)

**RESOLVED**: reports now age out. Each `REPORTED` row is weighted
`0.5 ** (age_days / 180)` and, below `GAME_REPORT_MIN_WEIGHT = 0.01` (~6.6 half-lives, roughly
3 years), drops out entirely.

**The expiry floor is the part that actually fixes it, and it is easy to miss** - I initially
shipped decay alone and a test caught that it does *not* break the ratchet: exponential decay is
asymptotic, so an old report leaves a photo at about -0.0000009, which still fails
`candidate_image_for_location`'s `effective_relevance(image) >= 0` gate. Excluded photos are never
shown, so they can never earn the "shown, no reaction" impressions that would lift them back up -
the score has to reach *exactly* zero for the loop to break.

Only reports decay. Thumbs up/down and no-reaction are still counted in bulk with one grouped
query: a report is a rare deliberate act (cheap to read row-by-row), while `NO_REACTION` accrues on
every impression and would be thousands of rows per popular photo. A freshly re-reported photo
stays excluded regardless of how old its other reports are - covered by a test, since "decay
amnesties a photo people are actively reporting" would be the obvious way to get this wrong.

The pre-existing `test_game_report_counts_at_full_negative_weight` moved from `assertEqual(-1.0)`
to `assertAlmostEqual`, because decay now starts immediately. 26 tests pass.

**Still open** (the second half of this entry): the empty state does not distinguish "no photos at
all" from "photos exist but community feedback filtered them", so the exclusion remains
undiscoverable in the UI. That needs a sentinel threaded from `candidate_image_for_location`
through the controller into `_empty_state.html`. Original entry below.

Reported symptom: after playing one full solo session against a ~10-pin pool, starting a new
session sometimes shows the empty state ("Nothing to play yet") even though the profile clearly
has pins and no restrictive settings are active.

Root cause: `services.spotguessr.photos.candidate_image_for_location()`'s default
(`allow_arbitrary_external_photos=False`) excludes any externally-sourced candidate photo whose
`services.media.media_relevance.effective_relevance()` score is negative. That score is fed by
`GamePhotoFeedback` rows (`services.spotguessr.relevance`) - thumbs-down/report reactions from
*any* past session, against *any* profile - and those rows never expire or get reset. For a
small pin pool where most or all locations have exactly one candidate photo, a handful of
thumbs-down votes accumulated during ordinary play can permanently knock that pool's only
playable photos below the eligibility threshold in *every future session*, with no way for the
player to know that's what happened (the empty-state copy just says nothing matched their
settings).

This is a real, separate bug from the "nothing to play yet" UX/response-shape issues fixed
alongside this note (see `docs/designs/drafts/spotguessr.md` and `controllers.spotguessr
.SpotGuessrStartView`) - it's a photo-inventory/relevance-decay *policy* question (should
`GamePhotoFeedback`'s influence decay over time? should a location with zero remaining eligible
photos fall back to `allow_arbitrary_external_photos`-style leniency automatically rather than
requiring the player to discover and toggle it? should thumbs-down carry less weight than it
currently does for small pools specifically?) that's bigger than a UX pass should decide
unprompted. Not investigated further here; worth a dedicated look before it's reported again as
"the game stopped working."

## 2026-07-28: `_StoredRangeValidationMixin._resolve_range` fails mypy (`serializers.py:2741`) - RESOLVED

**Resolved 2026-07-28** by the session that owned the in-progress work (the PR #124 Codex-review
pass). Diagnosis below was correct, including that a `cast` was the wrong answer. The fix was to
make the mixin a real `serializers.Serializer` subclass rather than a bare mixin over `object`:
its only correct use *is* as part of a serializer (it reads `self.context` and chains through
`super().validate()`), so the base list is the honest place to say so, and it types `context` and
`validate` together. It declares no fields, so `_declared_fields` is unaffected.

A `TYPE_CHECKING`-conditional base (`_Base = Serializer if TYPE_CHECKING else object`) was tried
first and rejected by mypy - `Variable ... is not valid as a type [valid-type]` - in both the
conditional-expression and statement-level `if`/`else` forms. Worth knowing before reaching for
that idiom here again.

Original report follows.


Noted while running `mypy` on `external_api/serializers.py` as a regression check after the
memories-journal/safety-maps pagination-envelope fix and the OAuth consent screen (unrelated
changes - see Part 7 of `docs/notes/mobile_app_notes.md`). `git diff` confirms neither of those
touched `_StoredRangeValidationMixin`, `TripUpdateSerializer`, or `TripActivityUpdateSerializer` -
this is uncommitted, in-progress work on trip/activity range validation, presumably from a
concurrent session on this same checkout (per `CLAUDE.local.md`'s note that multiple agents may be
working simultaneously).

`_resolve_range` reads `self.context.get("instance")`, but the mixin is a plain class (not a
`serializers.Serializer` subclass) - mypy has no way to know `self` will actually be a `Serializer`
at the point it's mixed in via `class TripUpdateSerializer(_StoredRangeValidationMixin,
serializers.Serializer)`. The fix is a type hint at the mixin boundary (e.g. a `Protocol` with a
`context: dict` attribute, or having the mixin only ever appear via a small typed base), not a
`cast`. Left alone rather than fixed here, since it belongs to a feature this pass didn't touch and
guessing at its intended shape risks colliding with whoever is actively editing it.

## RESOLVED 2026-08-18: SearXNG (`search.jmann.me`) image search 403s after coming back up from an outage

The operator enabled `json` in the instance's `search.formats` (reported 2026-08-18), which is what
the API clients need; the 403s are gone. The `User-Agent` mitigation below was a guess at the cause
and stays as harmless hygiene.

**The more important half was on this side, and is fixed with it.** While the instance was 403ing,
`searxng_images.fetch` caught the failure, logged it, and then cached an empty result anyway - and
the *existence* of a `LocationCache` row is what marks a source as having run. So every pin whose
media was fetched during the outage cached "no photographs here" and kept it: the emptiness
outlived the outage, which is why media stayed empty for pins that should have had it. A failed
fetch now writes nothing, leaving the source to retry. `redata_site_conditions` had the same shape
(a total failure cached an empty dict) and got the same treatment - with a partial result still
cached, since the domains that answered are real data.

Note for the staging deployment: pins that cached an empty image result during the outage keep it
until that cache row is invalidated. They are not refetched automatically, precisely because the
row looks like a completed fetch - clearing `LocationCache` rows with `source="searxng_images"` and
an empty `items` list is the way to pick them up.

The original entry follows.

## Pre-existing test failures found while fixing CRIS media (2026-08-05) - FIXED

15 tests in `dashboard/tests/hypothesis/` failed on a clean `9a8c0f14` checkout, unrelated to that
work (confirmed by running them from a detached worktree at HEAD). All three causes are now fixed:

- **`test_delete_low_engagement_wikis.py` (11 tests)** - every one died on
  `CommandError: Unknown command: 'delete_low_engagement_wikis'`. The management command simply did
  not exist; the tests specified it completely (two independent criteria - at most 2 distinct pin
  owners, or no surviving user edit - dry run by default, `--yes` to delete, cascade to child
  wikis) and it has now been written to match. Its docstring references a
  `services.visits.safety.destination_wiki_activity` precedent that also does not exist anywhere in
  the tree, so the "active user edit" rule was instead mirrored from `WikiEditQuerySet.active` and
  `services.achievements.metrics`, which agree on it.
- **`test_sun_times.py` (3 tests)** - written before the weather chain gained its REData-first
  chokepoint (`services.apis.locations.weather_resolution`). They patched only the direct
  Open-Meteo/OpenWeatherMap gateways, so on any machine with REData credentials configured the view
  made a real outbound call and tripped `core/testing_network.py`'s guard. They now switch REData
  off explicitly, pinning the direct-provider branch they were always about; REData's own branch
  stays covered in `test_weather_resolution.py`.
- **`test_panel_api_interface.py::ParcelBuildingsApiPayloadTests`** - the fixture placed the parcel's
  "second building" 200 km away. `building_rows` correctly drops a building outside the property's
  real boundary, and a boundary gets *derived* as soon as the pin has a child - so the moment a test
  added a covering child pin, the far-away building vanished from the payload and `unpinned_count`
  read 0. Product code was right; the fixture now puts both buildings on the parcel.

## RESOLVED 2026-08-07: two pre-existing bugs surfaced by extracting base.html's comment utilities

**Both are now fixed** (commit follows the extraction). Recorded here because they had been
live in production inside `base.html`'s inline script, unnoticed and untestable, since the
mention feature was written.

**1. `@mention` autocomplete had no request cancellation - real and user-visible. FIXED.**
`fetchSuggestions` debounced at 200ms but never aborted an in-flight request, so two lookups
could be outstanding at once and whichever *responded* last won, not whichever was typed last.
Typing `@mil` then `@mill` on a slow connection could leave the dropdown showing results for
`mil` while the textarea read `mill`; picking one then inserted a location the user never
searched for. Reproduced first with a test that resolves two stubbed fetches out of order -
it showed `Stale Mil` before the fix, confirming the race was real rather than theoretical.
Fixed with an `AbortController` per lookup plus a `stillCurrent()` guard that discards any
response whose query no longer matches the caret's fragment. The guard is the part that
actually matters: aborting is an optimisation, but a response can already be in flight past
the point where aborting helps.

**2. Mention insertion produced a double space - cosmetic. FIXED.**
`insert()` unconditionally appended a space, so inserting mid-sentence gave two:
`go @mill tomorrow` became `go @[Old Mill](loc:u1)  tomorrow`. Now the separator is skipped
when the following text already begins with whitespace.

## RESOLVED 2026-08-15 (chunk 462): 58 hand-declared indexes duplicate the ones Django already creates

Every `ForeignKey` gets `db_index=True` by default, so Django creates a single-column B-tree for it
automatically (`<table>_<column>_<hash>`). 25 model files additionally declare an `idxdb_*` index on
that *same single column*, producing two byte-identical indexes:

```
CREATE INDEX idxdb_pin_profile                       ON public.dashboard_user_pins USING btree (profile_id)
CREATE INDEX dashboard_user_pins_profile_id_7b152920 ON public.dashboard_user_pins USING btree (profile_id)
```

58 such pairs, verified against a fully-migrated database by comparing `pg_index.indkey` column lists
rather than index names, excluding partial indexes (`indpred IS NULL`), unique indexes, and
`varchar_pattern_ops` variants - the `_like` indexes Django creates for `LIKE` prefix matching are
*not* redundant with a plain btree and must not be swept up in this.

The cost is not the 816 kB they occupy on an empty database; it is write amplification. Every
INSERT, UPDATE and DELETE on those 25 tables maintains a second identical B-tree forever, and every
VACUUM and ANALYZE walks it. There is no read benefit whatsoever: the planner cannot prefer one over
an identical twin.

Distinct from the composite-prefix case. An index on `(a)` alongside one on `(a, b)` is *also*
redundant for lookups on `a`, and roughly 20 more of those exist - but dropping those is a judgement
call, because the narrower index is smaller and cheaper to scan. The 58 listed here are exact
duplicates with no such trade-off.

**Update (chunk 482, 2026-08-15): the composite-prefix set is systematically 62, not ~20** - the
full table (file, composite name, columns, redundant FK prefix) is in the audit report's
chunk-482 entry. After chunk 462 dropped the 58 exact duplicates, the redundant member of each
remaining pair is the FK *auto*-index (the composite covers its prefix lookups); dropping one
means `db_index=False` on that FK plus a migration. **Deliberately not dropped**: whether a given
auto-index earns its write cost depends on production scan counts. Decision procedure per pair:
check `pg_stat_user_indexes.idx_scan` for the auto-index on a production-shaped database; if the
composite absorbs those scans (it will, for pure prefix lookups, unless the planner prefers the
smaller index under memory pressure), set `db_index=False`. Judgement is the owner's, with data.

Not fixed in this pass, deliberately. It means editing 25 model files plus a migration dropping 58
indexes, and this audit's working tree already carries 219 changed files; a schema migration of that
size buried inside it makes the whole changeset harder to review and riskier to land. It is also
worth the owner choosing when index drops hit production, even though each one is individually safe
and trivially reversible (the identical twin remains, so no query plan can regress).

To redo the query, or to regenerate the list: see the audit report's chunk-162 entry.

**Resolved (chunk 462, 2026-08-15).** All 58 re-verified statically (each single-column on a
ForeignKey with its automatic index intact - the formatting-tolerant check is in the audit
report's chunk-462 entry), the declarations removed from 25 model files, and migration
`0045_drop_duplicate_fk_indexes` generated by autodetection: exactly 58 `RemoveIndex` ops,
depending on 0044. `makemigrations --check` is clean and a fresh test database builds through
0045. Each drop is individually reversible (the twin remains) and the migration itself reverses
by re-adding. The ~20 composite-prefix near-duplicates stay untouched, as the entry argued.

Full list of the redundant (`idxdb_*`) indexes:

- `idxdb_album_pin`
- `idxdb_album_wiki`
- `idxdb_albumitem_album`
- `idxdb_albumitem_image`
- `idxdb_bv_place`
- `idxdb_cl_pin`
- `idxdb_cl_profile`
- `idxdb_cl_wiki`
- `idxdb_dmlocm_message`
- `idxdb_ecd_owner`
- `idxdb_evp_visit`
- `idxdb_label_profile`
- `idxdb_loc_gplace`
- `idxdb_loc_place`
- `idxdb_mm_pin`
- `idxdb_mm_profile`
- `idxdb_mms_markup_map`
- `idxdb_mms_to_profile`
- `idxdb_pag_profile`
- `idxdb_palias_pin`
- `idxdb_pin_location`
- `idxdb_pin_parent_pin`
- `idxdb_pin_profile`
- `idxdb_pinlist_profile`
- `idxdb_pinowner_pin`
- `idxdb_place_domain_root`
- `idxdb_place_parent`
- `idxdb_pli_list`
- `idxdb_pli_pin`
- `idxdb_plink_pin`
- `idxdb_pm_layer`
- `idxdb_pm_map`
- `idxdb_pm_pin`
- `idxdb_pm_profile`
- `idxdb_pm_wiki`
- `idxdb_pn_pin`
- `idxdb_pv_pin`
- `idxdb_react_dm`
- `idxdb_react_gmsg`
- `idxdb_react_trcomment`
- `idxdb_route_profile`
- `idxdb_savedfilter_profile`
- `idxdb_scanentry_device`
- `idxdb_scc_checkin`
- `idxdb_scm_checkin`
- `idxdb_scoo_checkin`
- `idxdb_scoo_owner`
- `idxdb_scoo_profile`
- `idxdb_soc_link_pfile`
- `idxdb_ta_trip`
- `idxdb_taar_activity`
- `idxdb_tav_activity`
- `idxdb_tc_trip`
- `idxdb_tm_trip`
- `idxdb_walias_wiki`
- `idxdb_we_wiki`
- `idxdb_wiki_parent_wiki`
- `idxdb_wlink_wiki`

## RESOLVED 2026-08-13: `Label` has no uniqueness constraint, and nine sites `get_or_create` on it

**Resolved** by migrations 0042 (merge duplicates) and 0043 (add the constraint), plus graceful
conflict handling on every write path. See the audit report's Label uniqueness entry.

### Original report

`Label` declares no `unique`, `unique_together` or `UniqueConstraint` at all - its only unique
indexes are `id` and `uuid`. Nine non-test call sites nonetheless treat `(profile, name, kind)` as
though it identified a row:

- `models/labels/signals.py` x5 (seeding a new profile's default statuses/categories)
- `models/pin/model.py:833`, `models/wiki/model.py:328` (`kind`+`name`, global labels)
- `services/media/media_labels.py:99`, `services/apis/locations/google/maps.py:1150`,
  `controllers/pin_edit.py:357`, `tasks.py:1585`

Two consequences, one worse than the other:

1. **Race.** `get_or_create` is a `SELECT` then an `INSERT` with no constraint to lose against, so two
   concurrent requests - two import tasks, or a profile-creation signal racing a first pin save -
   both miss and both insert. The user ends up with two labels of the same name, and later
   `.get(name=...)` calls raise `MultipleObjectsReturned`.
2. **`media_labels.py:99` shows the workaround already in the tree**: it does a
   `filter(name__iexact=...).first()` *before* falling back to `get_or_create`, because
   `get_or_create(name=...)` is case-sensitive while the intended identity is not. That is a
   case-insensitivity fix layered on top of a missing constraint - and the fallback path can still
   race.

`PinAlias` and `WikiAlias` model the intended thing correctly, with
`UniqueConstraint(Lower("name"), <parent>)`. The same shape on `Label` -
`UniqueConstraint(Lower("name"), "profile", "kind")` plus a partial variant for global labels where
`profile IS NULL` - would make all nine sites safe and let `media_labels.py` drop its pre-filter.

Not fixed here: it needs a data migration to merge existing duplicates before the constraint can be
added (adding it to a table that already violates it fails), and deciding how to merge two labels
that differ only by case is a product call - the label merge machinery exists (`services/labels`),
but which name survives is not something to guess at.

## RESOLVED 2026-08-15 (chunk 490): two label lookups match on name alone, ignoring kind

`services/apis/locations/google/maps.py:1150` and `tasks.py:1416` both do:

```python
Label.objects.get_or_create(
    profile=user_profile,
    name__iexact=stem,
    defaults={"name": stem, "kind": "category"},
)
```

`kind` is in `defaults`, not in the lookup - so the `get` half matches on
`(profile, lower(name))` across **every** kind. A user with a *tag* called "Factory" who imports a
Google Maps list named "Factory" gets that tag returned and used as the list's category: the pin is
filed under a tag, and no category is created.

Predates the uniqueness work and is unaffected by it - the constraint is per-kind, so nothing here
violates it. The fix is to move `kind` into the lookup, which is what the equivalent code in
`controllers/pin_edit.py` and `services/media/media_labels.py` already does:

```python
Label.objects.get_or_create(profile=..., name__iexact=stem, kind="category", defaults={"name": stem})
```

Not changed here because both sites are on the Google Maps import path, which has its own
category-creation semantics worth reading before altering (`create_category`/`stem` come from the
imported list's title), and this audit had no test data exercising a cross-kind name collision.

**Resolved (chunk 490, 2026-08-15).** `kind="category"` moved into the lookup at both sites,
matching `pin_edit`/`media_labels`' existing pattern; the missing cross-kind test now exists
(`test_import_category_label_kind.py`) and pins both directions: a same-named tag is never
mistaken for the category, and an existing category is reused case-insensitively. 560
label/google-maps tests pass with the change. Bonus finding recorded for future fixtures: new
profiles are *seeded* with default labels (including a "Factory" category), which a test
creating labels by hand can collide with.

## RESOLVED 2026-08-18: "detach location" on a pin fails with a 500, every time

**Resolution: the third filed option - detaching was never coherent, and the handler now says so
with a 400 instead of raising an IntegrityError.**

The first attempt at this fix reasoned that a pin attaches to a *nearby* Location, so detaching
would be meaningful whenever the pin sat at a point the shared record did not occupy. That is
wrong, and the schema says so in two places:

- ``Pin.effective_latitude`` returns ``self.location.latitude`` - a pin has no coordinate of its
  own, whatever ``docs/NOTES.md`` implies about "marker coordinates".
- A database trigger, ``dashboard_locations_freeze_identity``, makes a Location's coordinates
  immutable ("Get-or-create a new Location for the changed coordinates instead of mutating this
  row"), so a location cannot drift away from its pins either.

A pin's point is therefore always *exactly* its location's point, and "give this pin its own
Location at the same place" cannot be satisfied without moving the pin. Nudging the coordinates was
rejected for that reason: silently moving somebody's pin to satisfy a database constraint is a
worse surprise than being told the action does not apply. A pin that should not share a place's
record wants a *different* place, which relinking already does.

Covered by ``test_pin_detach_location.py``, which pins the two things a refusal must not do (change
the pin's location, leave an orphan Location behind) and asserts both schema facts the decision
rests on, since either could be quietly relaxed later.

The original filing follows.

## RESOLVED 2026-08-15 (chunk 488): secondary-email verification is an unbounded send to arbitrary addresses

`ProfileEmailsView` (`controllers/userprofile.py`) sends a verification email to any address a user
types, through two paths:

- `_add_email` (line 769) - add a secondary email, then send;
- `_resend_email_verification` (line 781) - resend to a pending one, with no cooldown.

Both validate the address, reject one already in use, and reject one this profile already added.
None of that bounds *volume*. There is no rate limit, no cooldown on resend, and no cap on how many
secondary emails a profile may hold - `grep` finds no `max_secondary_email`-style setting, and the
`SiteSettings` limits that exist for friends, trips, lists and check-in contacts have no counterpart
here.

That makes it the fourth path that mails an arbitrary address, and the only unbounded one. The other
three are all governed by `services/security/email_safety`: friend invites and visit invites call
`email_rate_limit_error` + `has_sent_join_email` + `record_email_sent` (per-profile hourly/daily/
monthly caps, one join email per address ever), and safety-contact alerts are capped per check-in
with an opt-out. `EmailType` has exactly two members - `JOIN_INVITE` and `VISIT_INVITE` - so this
send type is not even representable in the ledger that enforces those caps.

Two distinct abuses, both cheap:

- **relay**: add N distinct addresses, each triggering one mail from your domain;
- **mail-bomb**: repeatedly POST the resend action for one pending address.

The fix is small because the machinery exists: add an `EmailType.EMAIL_VERIFICATION` member, call
`email_rate_limit_error(profile)` before sending and `record_email_sent(...)` after, and give resend
a cooldown. Filed rather than done because the per-type limits are `SiteSettings` values the owner
sets, and picking numbers for a new category is their call.

**Resolved (chunk 488, 2026-08-15) - the deferral premise was wrong**: `email_rate_limit_error`
enforces the owner's existing *per-profile* hour/day/month caps across all types - no new numbers
exist to pick. `EmailType.EMAIL_VERIFICATION` added (migration 0046), both send paths now guard
with the ledger and record their sends, and resend has a fixed 5-minute per-address cooldown (a
code constant like the notification debounce, deliberately not configurable). A blocked add
creates no pending row. Both abuse shapes (relay, mail-bomb) closed; 4 tests.

**How this was missed the first time** (audit chunk 170): that pass grouped the 21 send sites by
file, then read the recipient expression for only three of them, and reported the classification as
if it covered all 21. The same silent-sample error as the controller-create sweep. Re-reading the
remaining six recipients is what surfaced this.

## RESOLVED 2026-08-16: `Pin.icon` is unvalidated free text rendered into a `src` attribute

**Fixed 2026-08-16**, following the `services/core/colors.py` model this entry named: one helper,
`services/core/icons.clean_icon`, applied at every write path. It accepts exactly the three shapes
the field is meant to hold - a Material Icons name (`^[a-z0-9_]+$`), an uploaded icon's URL
(`^(https?://|/)` with no whitespace, quotes, angle brackets or backticks), or a short emoji token
(≤12 code points, every one a non-ASCII symbol/mark/joiner) - and coerces anything else to the
caller's default rather than raising, matching `clean_color`'s reasoning about pickers.

Applied at four sites, chosen so each covers several callers rather than one:
`services/pins/pin_creation.create_pin_for_profile` (the map's add-pin dialog, the external API's
pin create, and the import paths), `services/pins/pin_edit.apply_pin_edits` (the website's edit
dialog and the API's PATCH), `controllers/pin_bulk` (bulk style edit, one branch over from where
colours were already cleaned), and `controllers/detail_pins` (detail pin/child wiki create+update).
`controllers/maps` quick-edit cleans directly.

Covered by `test_icon_safety.py`, including a Hypothesis property that whatever survives `clean_icon`
fits the column and is classifiable by the renderers' own `is_icon_url`/`is_material_icon` filters -
so nothing can reach the `<img src>` branch without having passed the URL test.

Not changed: `Label.icon` and the other icon columns. They render through the same filters and are
the same class of value, but their write paths have their own defaults and protected-label rules;
converting them is a follow-up, not part of this fix. The ~60 further attribute interpolations
below are also untouched, and for the same reason as before - their values are not user-controlled.

The original entry follows.

## RESOLVED 2026-08-16: `Pin.icon` is unvalidated free text rendered into a `src` attribute

`Pin.icon` is `CharField(max_length=255, null=True, blank=True)` - no validator, no choices - and
is assigned straight from request data by the pin write paths, exactly as colours were before
`services/core/colors.clean_color`. The map page renders it into `<img src="...">`.

The client side is fixed (`_ulEscAttr` in `pages/map/index.html`), and the pre-existing
`/^(https?:\/\/|\/)/` test in front of it blocks `javascript:`. What is missing is the server-side
half, which is where the colour equivalent ended up:

- an icon value should be validated on write (a URL, a relative media path, or an emoji/short
  token - whichever the field is actually meant to hold; it currently holds all three depending on
  the code path reading it)
- `services/core/colors.py` is the model to follow - one helper, applied at every write path

Related: the same sweep found ~60 further attribute interpolations across templates and TS that
were *not* changed because the interpolated values are UUIDs, integer ids or enum keys. They are
listed by grepping for `="' +` / `="${` in `dashboard/templates` and `dashboard/frontend/ts`. If a
future change makes any of those values user-controlled, they become the same bug.

## RESOLVED: `Pin.to_json()` scaled linearly in queries; now flat

**Measured, 2026-08-14.** Serialising pins fetched with the prefetches a caller would reasonably
supply:

| | 1 pin | 5 pins | per pin |
|---|---|---|---|
| original | 6 | 22 | **4** |
| after the labels fix | 4 | 12 | **2** |
| after the rating fix | 3 | **3** | **0** |

Two independent causes, and they share a root worth internalising:

1. `self.labels.filter(kind=...)`, twice - `.filter()` on a prefetched m2m builds a fresh queryset.
2. `Pin.rating` used `self.reviews.all().latest()` - `.latest()` appends ORDER BY + LIMIT and so
   always queries. `Review.Meta` sets `get_latest_by = "created"`, reproduced exactly by
   `max(reviews, key=lambda r: r.created)`; getting that key wrong would have silently changed which
   review's rating is shown, which is a correctness bug wearing a performance fix's clothes.

**The general rule: only `.all()` reads a `prefetch_related` cache.** `.filter()`, `.latest()`,
`.count()`, `.exists()` and `.first()` on a related manager all issue a query regardless. Two of
those appeared in a single method here.

### Swept: no other serialisation method has this problem

139 serialisation-ish methods (`to_json`/`serialize`/`as_dict`/`to_dict`/`_row`/`_payload`) checked
for `self.<related>.<verb>(...)` where the verb bypasses the cache. **Zero hits.** `Pin.to_json`
was the only one, and only one method in the codebase now uses the cache-friendly
`self.<related>.all()` form - the one this work introduced.

The first version of that sweep also returned zero, and was wrong. It required the verb to follow
the relation *immediately* (`self.reviews.latest()`), so it missed the chained form
`self.reviews.all().latest()` - which is one of the two bugs it was written to find. Corrected to
match a verb anywhere in the chain, with both known bugs as controls; only then is the zero
evidence.

The runtime instrument remains the better one for anything this cannot see:
`dashboard/tests/hypothesis/test_pin_to_json_prefetch.py` captures queries over 1 and N objects and
asserts the per-object delta is zero. That measures the property directly rather than pattern-
matching its likely spellings.

---

## RESOLVED: `PinViewSet` prefetches labels and reviews

`models/pin/viewset.py` did `select_related("location")` only, while the serializer exposes
`rating`, `categories`, `tags` and `statuses` - each of which reads a related manager per pin.

The open question from the previous pass is answered: `categories`/`tags`/`statuses` come from
`models/abstract/labelled.py`, whose `_labels_of_kind` is

    [label for label in self.labels.all() if label.kind == kind]

- **already the cache-friendly form**, with a docstring noting the order "a caller's `Prefetch` may
have ordered". The abstract base had this right all along. So the three label fields cost nothing
*given* a prefetch, and one query each per pin without one.

`prefetch_related("labels", "reviews")` added, justified per field rather than guessed.

### Worth noting for the wider codebase

`Pin.to_json()` reimplemented `_labels_of_kind` badly - it used `self.labels.filter(kind=...)`,
which bypasses the cache, when the mixin it inherits from already provided the correct version. The
2026-08-14 fix made `to_json` match what `Labelled` had been doing correctly all along. Any other
model method filtering `self.labels` directly should use the inherited property instead; that is a
one-line grep (`self.labels.filter(kind=`), run 2026-08-14: **one hit**, at
`models/pin/model.py:811` inside `change_category`. It is a *write*
(`labels.remove(*self.labels.filter(...))`) executed once per request rather than per row, in a
method this audit found has no production callers - so it is not worth changing. No serialisation
path retains the trap.

---

## RESOLVED (already fixed in `0401aa2a`; entry was stale as of 2026-08-16): `LabelReorderView.post` issues one UPDATE per label

`controllers/labels.py:848` already does what the entry recommends, and went further than it asked:
it takes `bulk_update` (the option argued for below), writes **only** the rows whose order actually
moved, and calls `refresh_map_pin_cache_for_label_ids` because `bulk_update` fires no `post_save` -
which is the receiver trap the entry flagged, resolved rather than merely noted. Covered by
`test_label_reorder_query_count.py` and `test_label_reorder_refreshes_map_cache.py`; the "ids not
belonging to the profile are silently ignored" behaviour is preserved by the scoped fetch.

The original entry follows.

## RESOLVED: `LabelReorderView.post` issues one UPDATE per label (now `bulk_update`, covered by `test_label_reorder_query_count`)

`controllers/labels.py`, in the never-executed set from the coverage run:

    for i, label_id in enumerate(label_ids):
        Label.objects.filter(id=label_id, profile=profile, kind=self.kind).update(order=total - i)

Drag-and-drop reordering of 50 labels is 50 `UPDATE` statements. This is an N+1 on the **write**
side, which the audit's earlier sweeps did not look for - they targeted reads.

The handler is otherwise careful: kind is validated against `_ORGANIZE_KINDS`, the JSON parse
catches `JSONDecodeError`/`ValueError`/`AttributeError`, and every query is scoped to
`profile=profile, kind=self.kind`, so a crafted payload cannot reorder someone else's labels.

Two options, and the choice matters:

- **`bulk_update`** - fetch the scoped labels once, set `.order` in Python, `bulk_update(labels,
  ["order"])`. Two queries, idiomatic, and keeps the scoping in the fetch.
- **`Case`/`When`** in a single `update()` - one query, but generates SQL proportional to the
  number of labels, which is unpleasant at a few hundred.

`bulk_update` is the better default here. Note it does **not** fire `post_save`, and `Label` has
receivers (`sync_redata_taxonomy_on_save`) - check whether an order-only change needs them before
switching. That is the same trap recorded for the seeding loop in `labels/signals.py`.

Untested, so any change wants a test first: the existing behaviour to preserve is that ids not
belonging to the profile are silently ignored rather than erroring.

## RESOLVED 2026-08-15: `account.py` cites an unfiled decision about raw-password validation

**Resolved (chunk 454, 2026-08-15): the decision family now has a tracked record.** The four
2026-07-23 decisions (per-recipient payloads, opaque identifiers, wire them all, option (a))
were reconstructed from the citing comments' own summaries into "Decisions from the 2026-07-23
session (reconstructed)" in `docs/NOTES.md` - explicitly labeled a reconstruction - and all six
citing comments now point there instead of at this file.

`controllers/account.py` (~line 1136) documents a security-sensitive choice:

> The raw password crosses HTTPS exactly once here, is validated in memory, and is never stored or
> logged (decision 2026-07-23, `docs/PROBLEMS.md` - option (a): a validation endpoint, rather than
> duplicating every validator's rules in TypeScript and keeping them in sync by hand).

**That decision is not in this file.** Full-text searches for "option (a)", "validation endpoint",
"breach check", "raw password" and "password validat" all return nothing, and none of the 18
`2026-07-23` mentions covers it.

This matters more than the other dangling references found today, because the citation is doing
*justificatory* work: it tells a reader that sending the raw password to the server was chosen
deliberately over a client-side alternative, and points at reasoning that cannot be read. Someone
reviewing this later gets an assurance they cannot check, which is the position a security comment
should never leave a reviewer in.

The comment is self-contained enough to stand on its own - the argument (avoid duplicating validator
rules in TypeScript) is stated inline. The missing piece is whatever weighed option (a) against the
alternatives, including the ones not named here.

## RESOLVED 2026-08-15: the "wire them all" WhatsApp/SMS decision is cited twice and filed nowhere

**Resolved (chunk 454, 2026-08-15): the decision family now has a tracked record.** The four
2026-07-23 decisions (per-recipient payloads, opaque identifiers, wire them all, option (a))
were reconstructed from the citing comments' own summaries into "Decisions from the 2026-07-23
session (reconstructed)" in `docs/NOTES.md` - explicitly labeled a reconstruction - and all six
citing comments now point there instead of at this file.

Two files cite a decision that is not in this document:

- `services/notifications/notification_text_alerts.py` - "every other toggle was stored and silently
  ignored (docs/PROBLEMS.md; **decision 2026-07-23: wire them all**)";
- `models/notifications/signals.py` - the same situation, "silently did nothing (docs/PROBLEMS.md)".

Searches for "wire them all", "_whatsapp", "_sms opt-in" and "silently ignored" find only a naming
issue (2026-08-11, enum member vs value) and code snippets - nothing recording the decision to wire
every `<type>_whatsapp`/`<type>_sms` toggle through.

The related entries that *do* exist are narrower: a RESOLVED one about alerts never firing for safety
check-in partner invites, and a coverage note that 20 of 32 notification types have no per-type
delivery control. Neither is the decision, which is why an earlier attempt to place the
`signals.py` reference could not choose between them - **the correct answer was that neither
matched.**

The code implementing the decision exists and its docstring explains the reasoning inline, so nothing
is unexplained. What is missing is the record the two comments assert exists.

## RESOLVED 2026-08-14: `completed.md` is referenced from three places and does not exist (it is gitignored)

Chasing the unfiled 2026-07-23 decisions led here. `docs/PROBLEMS.md` (~line 1508) points at
`docs/notes/ai/completed.md` for "the whole PR #111 cluster"; `CLAUDE.local.md` points at
`docs/prompts/completed.md` for previous agents' work. **`find docs -name completed.md` returns
nothing** - neither path exists.

Consequences, in order of how much they cost:

1. The six comments citing "decision 2026-07-23" (per-recipient payloads, opaque identifiers, wire
   them all, option (a)) point at reasoning that is now in no file under `docs/`. Searched all of
   `docs/` for each phrase - the only hits are where this audit quoted them today.
2. Anyone following the `PR #111 cluster` pointer, or `CLAUDE.local.md`'s guidance to read what
   previous agents did, gets a missing file rather than an empty one - which reads as a broken
   checkout rather than absent history.

**Resolved (chunk 388): it is gitignored, not missing.** `.gitignore:49` ignores `docs/notes/ai/`,
and `git log --all -- '*completed.md'` shows the file was never committed. It is a local-only agent
notes directory.

**So the real defect is structural: tracked documentation references gitignored content.**
`docs/PROBLEMS.md` is committed and shared; `docs/notes/ai/completed.md` can never be. Anyone who
clones this repository - or works in a different checkout, as this one is - gets a pointer to
reasoning they have no way to obtain. The six code comments citing "decision 2026-07-23" are in the
same position: the decisions may well be recorded, on whichever machine ran that session.

Two ways out, both cheap: move decisions worth citing into a tracked file when the session that made
them ends, or stop citing `docs/notes/ai/` from tracked files and code. The current arrangement
promises a record that most readers structurally cannot reach.

**Scope (chunk 389): this is not one stray pointer.** `docs/notes/ai/` is cited by **9 tracked
files**, including `docs/ROADMAP.md` and `docs/designs/place-consolidation.md` - not just this one.
A reader in a fresh checkout following any of them lands on nothing. `.venv_windows` (also ignored)
is cited by 3 tracked docs for the same reason.

The roadmap and design-document citations are the more consequential half: `PROBLEMS.md` entries are
usually self-contained enough to stand without their footnote, whereas a design document deferring
to an unreachable file may be the only place a decision was ever explained. If its
content survives in git history, recovering the 2026-07-23 decisions from it would close six
dangling code comments at once; if not, those decisions exist only as the one-line summaries in the
comments themselves.

## RESOLVED 2026-08-16: `test_only_submitted_fields_ever_move` fails in the full suite, passes alone and at module scope

**Two more of the same species (2026-08-15).** `test_spotguessr_socket_scopes.py::
GameSessionSocketScopeTests::test_a_session_connection_is_unaffected` (seen once, chunk 489) and
`test_safety_contact_revocation.py::ContactAccessRevocationTests::
test_owner_and_contact_exchange_messages` (seen once, chunk 505) each failed in one large
multi-module run and pass standalone *and* at module scope. In the safety case the only touching
change was an additive `aria-label` on two form controls - markup that cannot influence message
exchange - which rules out the obvious suspect and points at cross-module state, same as the
other two. Three now recorded; if a fourth appears, the shared cause is worth hunting properly
(candidate: a module leaving `cache`/`override_settings` state behind, since all three failures
involve state read at request time).

**Two candidates eliminated (chunk 506)**: the base `TestCase` already clears the Django cache in
`setUp` (`_CacheIsolationMixin`, whose docstring names this exact hazard), and `SiteSettings`'
process-level memo is armed only inside a request scope (`request_started`/`request_finished`)
and is not touched by any of the three flaky tests. So the shared cause is neither stale Django
cache nor a pinned settings row. Remaining candidates for whoever picks this up: connection-level
state that survives rollback (advisory locks, `SET LOCAL`), a module-scope `mock.patch` left
active by a failed cleanup, or Hypothesis' database of previously-failing examples interacting
with run order.

**Mechanism found (chunk 507) - the Hypothesis example database is real, shared, and
root-owned.** `/app/.hypothesis/examples/` exists in the test container with stored entries dating
from 2026-08-06 and 2026-08-15. Hypothesis replays previously-failing examples *first* on every
subsequent run, so a `@given` test that failed once keeps re-trying that input - which produces
exactly the observed signature: a failure that appears in one large run and vanishes in isolation
(different worker, different container state) with no code change between.

**Correction (chunk 508): this explains ONE of the three flakes, not all three.** Checked instead
of assumed: `test_only_submitted_fields_ever_move` *is* `@given`-driven (dictionaries of
permission fields x levels), so the replay mechanism fits it exactly. But
`test_spotguessr_socket_scopes.py` and `test_safety_contact_revocation.py` contain **no `@given`
and no `subTest`** - the example database cannot touch them, and their cause remains unknown.
Chunk 507's write-up said "all three involve `@given` or subtests", which was asserted rather
than verified and is false.

**The other two explained (chunk 509) - and fixed.** Both are WebSocket consumer tests
(`TransactionTestCase` + channels). Tests run against the **real Valkey channel layer**, and
channel-group names derive from model pks (`profile_notifications_<id>`, and likewise per
check-in/session) - while every test database restarts its sequences at 1. So `UL_TEST_DB_NAME`
isolates Postgres but **not** the channel layer: two concurrent runs address the same groups, and
a websocket test in one run can consume or lose a message belonging to the other. Both sightings
occurred in runs that overlapped another suite; neither ever reproduced alone.

Fixed by giving the test channel layer a per-run `prefix` derived from `UL_TEST_DB_NAME`
(`settings/base.py`, under `TESTING` only - outside tests the channels_redis default is
unchanged). Verified by running both previously-flaky modules *simultaneously* against different
test databases: 5/5 and 14/14, the exact configuration that produced the failures. This also
removes a real hazard for any parallel CI, not just this audit's concurrent runs.

Two aggravating details:

- The directory is **owned by root and mode 755**, while tests run as `appuser` - writes fail
  silently, so the store is *read-only in practice*: bad examples are replayed forever and newly
  discovered ones are never recorded. (`docker exec` defaults to root, which is how it came to be
  root-owned in the first place - the same footgun recorded for `logs/` in CLAUDE.local.md.)
- Nothing registers a Hypothesis profile, so this is the library default rather than a decision.

Three fixes, owner's choice: (a) `derandomize=True` or an explicit `database=None` profile for CI
determinism, (b) chown the directory to `appuser` so the store works as designed, or (c) delete it
and let it regenerate under the right owner. Recorded rather than applied because (a) changes
test-determinism policy for everyone and (b)/(c) touch a container whose state the owner manages -
the same reasoning as the dev-stack entries.

**Fixed in-repo 2026-08-16, without taking any of those three.** `src/urbanlens/conftest.py` now
registers an explicit `urbanlens` Hypothesis profile whose example database lives in a directory the
test user can actually write (`$TMPDIR/urbanlens-hypothesis-examples`, overridable with
`UL_HYPOTHESIS_EXAMPLE_DIR`; set it empty for an in-memory store). Writability is *proved* with a
probe file rather than assumed, since an unwritable inherited directory is the exact failure this
exists to avoid - if the probe fails it falls back to an in-memory database and logs a warning.

This takes (b)'s benefit without touching container state, and leaves determinism policy alone -
the store works as designed, so an example that stops failing is now removed instead of replayed
forever. The directory is deliberately stable across runs rather than keyed to `UL_TEST_DB_NAME`:
a per-run store learns nothing, and `DirectoryBasedExampleDatabase` is file-per-entry and safe for
concurrent readers/writers. The root-owned `/app/.hypothesis` is now simply unused; deleting it is
still worth doing but no longer fixes anything.

Nothing has reproduced `test_only_submitted_fields_ever_move` since - including the ninth
full-suite consolidation (2026-08-16, 10,885 passed / 1 xfailed / 0 failed).

`SetTripPermissionsPresenceTests::test_only_submitted_fields_ever_move`
(`test_external_api_trip_settings.py`) failed in the chunk-455 full-suite run (10,838 others
passed) and passes both standalone and with its whole module. Its traceback was not captured (the
run's output was truncated to the short summary), so the failing example is unknown. A
Hypothesis presence-test failing only under full-suite ordering fits the documented gotcha that
the test client keeps state across generated examples; suspect order-dependent state from an
earlier module rather than a product bug. Next full-suite run should capture full tracebacks for
this module (`-q` plus an un-truncated tail, or `--tb=long -k` on rerun) before anyone chases the
product code.

## RESOLVED 2026-08-16: the website's bulk pin endpoints were unbounded while every API equivalent capped at 500

Found in chunk 525's sweep for **write-side** N+1 - the class the `LabelReorderView` entry named
as unexplored ("an N+1 on the write side, which the audit's earlier sweeps did not look for - they
targeted reads"). An AST pass over every non-test `for` loop containing a write-shaped call
returned 267 loops, narrowed to 62 that write once *per item*. Most are legitimately per-item
(the safety escalation's per-contact stamp, `BackupCode`'s conditional claim, undo handlers bounded
by their entry). The bulk pin endpoints are the ones that matter, and the reason is not laziness:

**`Pin` carries eight live `post_save` receivers** - map-pin cache, smart-list membership, wiki
stat sync, draft-wiki creation, boundary refit, map-center invalidation, detail-pin resync, and the
achievements handler (read from Django's live signal registry, not by grepping for `@receiver`;
the entry above about the bulk-write guard says six, so it has grown by two). So these loops
*cannot* become one `bulk_update` without wiring all eight, and per-pin `save()` is load-bearing.

Which makes the bound the thing that matters. Measured rather than estimated, with
`CaptureQueriesContext` against a real database:

| bulk edit | queries |
| --- | --- |
| style (colour/icon/opacity), n=1 / 5 / 10 | 5 / 13 / 23 → **~2 per pin** |
| description, n=1 / 5 / 10 | 5 / 13 / 23 → **~2 per pin** |
| rating, n=1 / 5 / 10 | 10 / 38 / 73 → **~7 per pin** (`Review.update_or_create` plus its own receivers) |

Every external-API bulk endpoint already declares `max_length=500` on its uuid list
(`serializers_pin_bulk.py`: delete, merge, edit). **Not one of the internal endpoints had any
bound**, and the internal ones are what the map's select tool drives - so "select all, set a
colour" on a 5,000-pin account was ~10,000 queries in one request/response cycle, and a rating
edit ~35,000.

**Fixed** by giving `controllers/pin_bulk.py` a `_MAX_BULK_PINS = 500` applied to the three write
paths (delete and merge via the shared `_parse_uuids_json`, edit at its inline parse). The number
is not a new policy - it is the one the API already shipped.

**Read paths deliberately left unbounded**, because the cost model that justifies the cap does not
apply to them: `PinBulkExportView`'s own docstring says it uses a plain form POST specifically so
there is "no URL-length limit on the pin count", and it is one query plus serialization regardless
of selection size. `PinBulkEditLabelOptionsView` is likewise a single `pins__in` query. Capping
those would be copying the number to somewhere its reasoning doesn't reach.

### The refusal was invisible, which is half the bug

The map page's three bulk handlers each did `.then(r => { if (!r.ok) throw new Error(); ... })` and
caught with a fixed string - so a user selecting 600 pins would have been told "Update failed." with
no reason. They now go through `window.ulSendJson`, already used elsewhere in the same file, which
carries the server's message into the `catch`.

That exposed a real gap in the shared helper. `fetch-json.ts`'s `errorMessage` discarded **any**
non-JSON body as "an HTML error page… no use in a toast" - but this project answers refused writes
with bare `HttpResponse("...", status=400)` in many places, and that string is exactly the sentence
the user needs. It now keeps a short single-line plain-text body and still discards markup and
multi-line/over-long bodies. That silently improves every existing plain-text refusal in the app
("No pins specified.", "No matching pins.", "Description is too long."), each of which previously
reached the user as `HTTP 400`.

Covered by `test_pin_bulk_views.py::BulkSelectionSizeLimitTests` (including an anti-vacuity test
that exactly 500 still succeeds, and one asserting export stays unbounded) and 5 new cases in
`fetch-json.test.ts`. The single-pin `bulk_delete` call in the "cancel pin creation" dialog was
left alone: it sends one uuid, so the cap cannot reach it.

## RESOLVED 2026-08-16: `merge_pins`' `IntegrityError` recoveries could not run, and one of them was hiding a data-loss path

Found in chunk 526, continuing chunk 525's write-per-item list into `services/pins/pin_merge.py`.

The whole merge runs inside one `transaction.atomic()` block, and eight of its per-relation
reassignments were written as:

```python
try:
    row.save(update_fields=["pin", "updated"])
except IntegrityError:
    row.delete()          # drop the duplicate, carry on
```

Postgres aborts the **entire** transaction on a failed statement, so the recovery query itself
raises `TransactionManagementError: You can't execute queries until the end of the 'atomic'
block`. Every one of those graceful "auto-dedup" paths - the module docstring's whole middle
bucket, covering `PinAlias`, `PinOwner`, `PinAutoRemoval`, `PinShare`, `PinListItem` and `Review` -
was dead code. Any merge hitting a uniqueness collision failed outright instead of deduping.

**Reproduced against a real database, not inferred**: merging a pin into its own descendant, with
another top-level pin already occupying the location a child had to be detached to, raised
`TransactionManagementError`. (The transaction did roll back, so nothing was corrupted - the
failure was a confusing 500, not data loss.)

**Fixed** with `_save_within_savepoint()`, a nested `transaction.atomic()` (a savepoint) around
each risky save, returning whether the row was written. Only the failed statement rolls back, so
the caller's dedup rule can run - which is what the code always intended.

### The fix exposed a genuine data-loss path the broken transaction had been masking

`_reparent_children` detaches a child to top level when re-parenting it under the survivor would
close a loop - which happens precisely when **the survivor sits beneath that child**. When the
detach failed, the old code logged and continued, with a comment stating the child "remains
parented to the pin about to be deleted". `Pin.parent_pin` is `on_delete=CASCADE`, so
`loser.delete()` at the end of the merge would have destroyed that child *and the survivor
underneath it* - the exact outcome the detach exists to prevent, as its own docstring says.

That never happened only because the poisoned transaction raised first. Repairing the transaction
without addressing this would have turned a confusing error into silent destruction of the pin the
user was merging *into*. It now raises `PinMergeCollisionError`, which
`controllers/pin_merge_suggestions.py` surfaces as its own toast ("another top-level pin already
occupies its location") rather than the generic "Something went wrong" its `except ValueError`
would have produced - the user can act on this one by moving the blocking pin first.

### The rest of the class is clean

An AST sweep for `except (IntegrityError|DatabaseError|DataError|OperationalError)` handlers
lexically inside an `atomic()` scope (`with` blocks and `@transaction.atomic` decorators) found
four others, all safe for the same reason: each `raise`s or `return`s immediately rather than
issuing another query, so the block unwinds and Django rolls back normally.

- `controllers/e2ee.py:292` - returns 409 "a key bundle already exists". Returning from inside the
  block commits, but Postgres turns `COMMIT` on an aborted transaction into a rollback, which is
  the intended outcome anyway. Correct, if accidentally so.
- `services/consensus/session.py:307`, `services/spotguessr/session.py:646`,
  `services/trivia/session.py:370` - each raises a domain error ("already answered this round")
  out through the atomic block.

`pin_merge` was the only site whose handler kept working inside the aborted transaction, which is
what made it the only broken one.

Covered by `test_pin_merge_savepoints.py`, including a control test asserting that the *old* shape
really does poison the transaction - without it, the other tests would pass equally against a plain
`try/except` and would prove nothing about why the savepoint is there.

## RESOLVED 2026-08-16: the E2EE key reset could destroy preservable history, silently

Found in chunk 527, reading the E2EE "rewrap all" write loop from chunk 525's write-per-item list.
The loop itself is fine - it is bounded by the caller's own rows, each row's value differs, and the
bundle swap is guarded by a `select_for_update` version check whose comment shows the author was
already thinking about exactly this hazard class. What is around it was not fine.

A reset generates a new keypair and re-seals the account's conversation keys and group envelopes to
it. Re-sealing needs the **old** private key, so the payload is optional: someone who lost their key
cannot re-seal anything and resets purely to get a working account back, accepting the loss. That is
correct. Three things around it were not.

**1. A transient failure destroyed history the reset could have kept (`e2ee-client.ts`).**

```ts
if (oldPrivateKey !== null && cfg().urls.rewrapAll) {
    const rewrapResponse = await fetch(cfg().urls.rewrapAll, ...);
    if (rewrapResponse.ok) { /* build the rewrap payload */ }
}
// ...falls through and resets anyway
```

There was no `else`. Holding `oldPrivateKey` means every thread *could* have been preserved - so a
500 or a dropped connection on the inventory fetch reset the account to a new key and left the
entire history sealed to the retired one. Permanent, and caused by a network blip rather than by
the user's choice. It now aborts; the caller already renders "please try again", and a retry costs
nothing. The neighbouring per-entry skip ("entries that fail to unseal were already unreadable, so
leaving them behind loses nothing") is sound reasoning *per entry* and deliberately does not extend
to an inventory that never arrived - the comment now says so.

**2. The server never said what it left behind.** The response was `{version, rewrapped}`. A client
cannot compute the remainder without its own inventory - and in failure case 1 it has none. The
server knows exactly, so it now returns `not_rewrapped`: the caller's own conversation keys and
group envelopes still sealed to the retired key, counted after the swap. It is also logged.

**3. The toast lied in two of four cases.** `rewrapped > 0` produced "your message history was
re-encrypted - everything stays readable" even when 3 of 50 rows made it; and `rewrapped === 0`
with the old key held produced **no toast at all**, so a reset that had silently lost everything
looked identical to one that worked. The branch is now a pure exported `resetOutcomeMessage()`
covering all four combinations, tested without needing sodium or IndexedDB.

Also corrected while here: the endpoint's `@extend_schema` declared `E2EEOkResponseSerializer`
(`{"ok": true}`), which this endpoint has never returned. It now declares a real
`E2EEResetResponseSerializer`, with `not_rewrapped` documented as the signal that some of the
caller's threads are permanently unreadable.

Covered by four new cases in `test_e2ee.py` (including an anti-vacuity one that a complete rewrap
reports zero loss, and one that another profile's rows are never counted as the caller's) and
`e2ee-reset-outcome.test.ts`. What is still not covered is `resetKeys` end to end - it needs
sodium, IndexedDB and a live config, which is why the message logic was extracted to a pure
function rather than tested through it.

## RESOLVED 2026-08-16: "create a list and add these pins" did nothing at all on a duplicate name

Chunk 528 generalised chunk 527's E2EE bug into a sweep. That bug was *not* the already-swept
"mutating `fetch` with no `.ok` check" class - it had a check, and the failure branch was simply
empty. So this pass looked for the distinct shape: **a success guard whose failure path silently
falls through**, plus fire-and-forget chains with no `.catch` at all.

Two scans, both read rather than counted:

- 21 positive `if (resp.ok) { ... }` guards, 7 without an `else`. Six are the early-return idiom
  (the failure handling follows the block rather than sitting in an `else`) and are correct -
  `messages/index.html` ×3, `confirm-dialog.ts`, `_chat_panel.html`, `e2ee-client.ts`. Worth
  recording so the next sweep does not re-flag them: "no else" is not the signal, "no failure
  path" is.
- 144 promise-style fetch chains, 17 with no `.catch`, narrowed to 11 that are also fire-and-forget
  (the rest `return` the promise, so their caller owns the failure - the false-positive class the
  2026-08-07 sweep already recorded).

**The real finding, in two copies.** `createListAndAddPins` exists in both `pages/map/index.html`
and `pages/location/index.html`, and both did:

```js
}).then((r) => r.json()).then((data) => { if (data.ok) { ... } });
```

`lists.create` answers a duplicate name with **409 and the plain text** "You already have a list
with that name." - not JSON. So `r.json()` threw, into a chain with **no `.catch`**: an unhandled
rejection. The user clicked the button and *nothing happened* - no toast, no closed dialog, the
name still in the box. That is the likeliest failure this feature has.

The two copies had drifted, which made the second one worse than it looked: the location page has
an `else` that toasts "Could not create that list", and it is **dead code** - reaching it requires
the endpoint to return JSON with `ok: false`, which it never does. The map copy has no else at all.
The drift is visible fifteen lines away in both files: `addPinsToList`, immediately above, checks
`response.ok`, handles 409 explicitly, and toasts. Same file, same dialog, opposite care - the same
shape recorded on 2026-08-07 ("the map page already had `_fetchJson` doing `if (!resp.ok) throw`
while the two PATCH writes in the same file did not").

**Fixed** by routing both through `window.ulSendJson`, which since chunk 525 surfaces a short
plain-text refusal as the error message - so the user now sees the server's own sentence. Also
added the missing `.catch` to the pin-list reorder save (`pin_lists/detail.html`), where the new
order is already applied in the DOM, so a silent failure reads as a successful save until the next
page load undoes it.

Server-side, `lists.create` had **no tests at all**. `PinListCreateRefusalContractTests` now pins
the half of the contract the client depends on: 409 for a duplicate, a body that is short,
single-line and markup-free (the conditions `fetch-json.ts` requires to show it), that another
profile's identical list name does not block it, and that the success path still returns the uuid
the caller chains onto.

**Still unread from the fire-and-forget list** (9 sites): `map-annotations.ts:1712`,
`_photo_gallery.html:383`, `map/index.html:3928` (`addPinsToList` - checks `ok`, so only a network
error is silent), `memories/photos.html:401`, `settings/index.html:2331`, `trips/detail.html:1593`,
`location/index.html:979`, `pin_lists/detail.html:523`. Each needs judging on its own, exactly as
the 2026-08-07 entry concluded for the ~30 it left - several are legitimately best-effort.

## RESOLVED 2026-08-16: three confirmed, irreversible deletes reported nothing when they failed

Chunk 529, finishing the fire-and-forget list chunk 528 left unread. Nine sites, each judged on its
own as the 2026-08-07 entry concluded they must be. Three are real and share one shape, and it is
the worst place for it: **a destructive action behind a "this cannot be undone" confirm, whose
failure path says nothing at all.** The user cannot distinguish a delete that failed from one that
worked, because in both cases nothing on screen changes to explain it.

- `partials/pins/_photo_gallery.html` (`galleryDelete`) - `if (r.status === 204) { ...remove tile,
  toast success... }` with no else and no `.catch`. Anything but a 204 left the tile in place,
  silently.
- `pages/memories/photos.html` (`photosDelete`) - `if (!r.ok) return;`, an explicit early return
  with no message. Same outcome, arrived at deliberately-looking code.
- `frontend/ts/entries/map-annotations.ts` (`doDeleteSelectedDp`) - per-request `.then(r => r.ok)`
  inside `Promise.all`, with no `.catch`. It already toasts both "N deleted" and "N could not be
  deleted", so it handles a *refused* request well - but one **network** failure rejects the whole
  `Promise.all`, the async function throws, and the user gets no toast, no cleared selection and no
  refreshed list after confirming a bulk delete. `.catch(() => false)` per request routes it into
  the warning that was already there.

All three now report the failure. The gallery and memories fixes also gained the `.catch` they
never had.

**Judged fine, with reasons, so the next sweep need not re-derive them:**

- `pages/settings/index.html` autosave - **a false negative in my own scan**: it does have
  `.catch(function () {}).finally(...)`. The chain-extraction walked to the first `;` at what it
  thought was depth 0 and stopped early inside a nested `.then`. Swallowing the error is fine here:
  `schedule()` has already called `guard.markDirty()`, so an unsaved form stays dirty and the
  leave-page warning covers it.
- `pages/trips/detail.html` child-trip typeahead - a search suggestion read; a failure leaves the
  previous suggestions up, which is the standard degradation for a typeahead.
- `pages/pin_lists/detail.html:523` list-items refresh, `pages/map/index.html:3928` and
  `pages/location/index.html:979` (`addPinsToList`) - all three check `response.ok` and toast on a
  refusal; only a network error is silent, and the earlier fixed sites were the ones where silence
  followed an irreversible action.

**Scan-tooling lesson worth keeping** (the second this pair of chunks produced): a regex that
extracts a promise chain by walking to the first depth-0 `;` under-reports `.catch`, because a
nested callback body raises depth in ways brace-counting alone gets wrong. Treat "no `.catch`
found" as a candidate to read, never as a finding - which is how the settings false positive was
caught before it reached this file.

## RESOLVED 2026-08-16: a device-marker absence report could lose an increment, and revert a fresh detection

Chunk 530, finishing chunk 525's write-per-item list. Four sites left; two are clean, one is a
verified-safe area worth naming, and one was wrong.

**`services/device_scan/clustering.record_absence_report` - fixed.** It did:

```python
marker.absence_streak += 1
if marker.absence_streak >= ABSENCE_STREAK_THRESHOLD:
    marker.status = MarkerStatus.PRESUMED_REMOVED
marker.save(update_fields=["absence_streak", "status", "updated"])
```

Two defects in four lines:

1. **Lost update.** `process_device_scan_upload` claims each *upload* atomically, and its docstring
   explains that this stops a redelivered task "inflating a marker's absence streak a second time
   for the same physical report". That is real and correct, and it covers a different case from the
   one that bites: two *different* users' uploads naming the same marker, processed by different
   workers. Both read the same streak and write the same value, so one report vanishes and the
   PRESUMED_REMOVED escalation is delayed. Now `F("absence_streak") + 1`.
2. **Status stomp.** `status` was in `update_fields` unconditionally, so every absence report wrote
   back whatever status it had read - including on the ~9 of 10 calls that change nothing. An
   absence report landing just after a fresh detection set the marker ACTIVE would revert it to the
   stale value. `status` is now written only when it actually changes.

Neither is catastrophic - this is a community "is that camera still there?" signal, and the next
report self-heals it - but both are the exact shapes this audit has already fixed in
`FriendInvitation.mark_accepted` and the safety check-in transitions, so leaving them would keep a
wrong example in the codebase for the next person to copy.

**`services/consensus/tentative` - verified safe, and worth recording why.** `_record_text` and
`_record_coordinate` both do the same `existing.support_count += 1` read-modify-write. They are
safe because `record_tentative_answers`, their only caller (confirmed - nothing else in the tree
reaches the private functions), wraps both branches in `transaction.atomic()` holding
`select_for_update()` on the parent `Wiki`. Its docstring names this hazard exactly - "the
row-level `+=` loses an increment on top of that" - and explains why a unique constraint cannot
substitute: the coordinate branch dedups by *proximity*, which no constraint can express, and the
text branch's constraint is on `Lower(text_value)` while its lookup is on `normalized_text`. There
is already a `test_consensus_tentative_races.py` covering it. Twentieth verified-safe area.

**`services/photos/redata_relevance` and `clustering`'s STALE sweep - clean.** Both write one
`.update()` per row with per-row distinct values (so no bulk form exists), both are bounded by one
upstream batch or one wiki+device's markers, and neither goes through `save()`, so there is no
receiver question.

That closes the 62-site write-per-item list from chunk 525: the bulk pin endpoints (bounded, chunk
525), `pin_merge`'s recoveries (savepoints, chunk 526), the E2EE rewrap loop (fine; its *caller*
was the bug, chunk 527), and these four.

## RESOLVED 2026-08-16: nested archives each bought a fresh zip-bomb allowance

Chunk 531 opened a new thread - upload parsers and resource exhaustion. Most of it is genuinely
well built and worth recording as such, because the one gap is easy to miss among it: every
user-supplied XML path uses `defusedxml` (`gpx.py`, `gpx_tracks.py`, `osm_xml.py`, and `maps.py`
for KML, the last two pre-parsing with defusedxml purely to harden libraries that don't accept a
safe parser); `archive_extractor` verifies type by magic bytes, skips symlinks and path-traversal
entries, allowlists extensions, caps per-file and cumulative size and file count, and reads
`_MAX_SINGLE_FILE_BYTES + 1` rather than trusting a declared size; shapefiles go through that same
extractor rather than unzipping their own bundle.

**The gap is that those caps are per *archive*, and the upload path calls the extractor once per
nested archive.** `controllers/pin.py`'s import-preview endpoint expands an outer archive, then
loops over its entries and calls `extract_archive` again for each one that is itself an archive
(KMZ inside a ZIP is the legitimate case this exists for). Each of those calls started a **fresh**
2 GB / 1000-file allowance. An outer ZIP containing N nested bombs therefore cost N x 2 GB, and N
is itself bounded only by the outer archive's own 1000-file cap - so roughly 2 TB of decompression
and up to a million accumulated in-memory files, from one upload, inside one request. Nesting depth
is bounded at two (entries of the inner archive are taken as-is), which is the only reason this is
merely very bad rather than unbounded.

Fixed by making the allowance an object - `ExtractionBudget` - that `extract_archive` accepts and
threads into both extractors. The preview controller creates one per upload and passes it to every
call, outer and nested, so the whole upload shares one limit. Omitting it still gives a single
archive its own budget, which is correct for the callers that never nest.

### Checked and false: the declared-size "hole"

While fixing the above I believed a second bug: the cumulative counter accumulated
`info.file_size`, which is attacker-supplied, so a crafted entry declaring one byte while holding a
gigabyte would understate the total to nothing. **That is wrong, and I verified it rather than
shipping the claim.** CPython's `zipfile` bounds a read by the declared `file_size` and then
verifies the CRC: patching a 5 MB entry's declaration down to 1 yields `BadZipFile: Bad CRC-32`,
not 5 MB of data. The declared value was always a valid upper bound.

The change to charge actual bytes stands anyway - it is exact rather than conservative - but the
comments claiming it closes an attack were rewritten before commit.
`DeclaredSizeIsNotAnAttackVectorTests` now pins the real behaviour as a test rather than a note,
because the reasoning depends on CPython internals that could change.

### Noted, not changed: one corrupt member fails the whole archive

That experiment surfaced a robustness question. `_extract_zip` wraps its entire loop in
`except zipfile.BadZipFile`, and a CRC failure is raised by `f.read()` *per entry* - so a single
corrupt member inside an otherwise-fine 900-file Google Takeout archive aborts the whole extraction
with "Invalid ZIP archive" rather than skipping that member. Fail-closed is a defensible stance for
an importer and changing it is a product call about how much of a damaged archive to salvage, so it
is recorded rather than changed.

## RESOLVED 2026-08-16: the decompression-bomb fix reached one of two call sites

Chunk 532, continuing the parser thread into the image pipeline. Pillow's own `MAX_IMAGE_PIXELS`
ceiling is what prevents the memory exhaustion, and this codebase already knows the subtle part:
`DecompressionBombError` inherits straight from `Exception`, **not** from `OSError` like
`UnidentifiedImageError` does, so a `except (OSError, ValueError)` handler does not catch it. There
are two dedicated test modules about it.

An AST sweep of all 11 `PILImage.open` sites found 5 with no enclosing `try`. Reading them - rather
than reporting them - showed 4 are private EXIF helpers whose six call sites all catch bare
`Exception`, and the fifth (the photo-keywords plugin) is called inside a bare `except Exception`.
All fine.

**The real finding was the pair of call sites for `downscale_stored_image`:**

- `tasks.py:663` catches `(OSError, ValueError, PILDecompressionBombError)` and carries an eight-line
  comment explaining precisely why the third entry is required.
- `services/photos/photo_enrichment.py:102` calls the *same function* and caught only
  `(OSError, ValueError)` - exactly the handler that comment says is insufficient.

So a photo over 89 MP materialised from an external source (Wikimedia, Flickr, Yelp) raised out of
the enrichment run instead of degrading to the logged warning every other unprocessable image gets.
The evidence that this is a defect rather than a judgement call is in the repository itself: the
sibling call site documents the bug that the second one still had.

Fixed, with the comment naming where the reasoning came from. Covered by
`EnrichmentPathBombHandlingTests`, including an anti-vacuity test that `downscale_stored_image`
really does raise `DecompressionBombError` under a lowered ceiling, and a structural one asserting
that error is neither an `OSError` nor a `ValueError` - which is the whole reason a two-tuple
handler was not enough, and would fail loudly if Pillow ever changed the hierarchy.

**Method note.** The AST sweep was intra-function, so "no enclosing try" was never a finding, only a
candidate: 5 candidates, 4 dissolved on reading, and the one that mattered was not among them at
all - it was found by following the *function* to its callers rather than the `open()` to its
handler. Consistent with the two scan lessons recorded above it.

## RESOLVED 2026-08-16: the AI document import's size cap measured the wrong thing for .docx

Chunk 533, continuing the parser thread. `services/ai/document_import` is carefully bounded on
paper - 2 MB per upload, 20,000 characters of extracted text, 200 extracted pins, 500 KB of AI
response - and each of those limits is real. The gap is that two of them measure *different things*
and nothing measures the middle:

```python
if len(data) > MAX_DOCUMENT_BYTES:      # bounds the bytes uploaded
    raise DocumentTooLargeError(...)
text = extract_text(filename, data)     # <- the whole document is materialised here
if len(text) > max_chars:               # bounds the text, after the memory is spent
    raise DocumentTooLargeError(...)
```

For `.txt` those two are the same quantity, which is presumably why it read as sufficient. For
`.docx` - the only other supported format, and a ZIP - they are not. A 2 MB `.docx` whose
`word/document.xml` decompresses to gigabytes passes the first check, and `python-docx` builds the
entire part before there is any `text` to measure. The character limit is checked after the damage.

Reachability is ordinary rather than exotic: XML is repetitive and repetition compresses, so a
valid Word file with millions of repeated elements reaches four-figure compression ratios without
any special crafting. The endpoint is authenticated but the feature is available to any AI-enabled
profile.

**Fixed** with `_reject_oversized_docx`, which sums the sizes the ZIP directory *declares* and
refuses above 20 MB - generous by design, since a document with a 20,000-character text limit is
three orders of magnitude below it and only a bomb approaches it. Nothing is decompressed to
perform the check.

Checking declared sizes is sound here specifically because of what chunk 531 verified: CPython's
`zipfile` bounds a read by the declared size and then fails the CRC, so an understated declaration
cannot smuggle bytes past the check - it makes `python-docx` read a truncated part and raise, which
the existing `except Exception` already turns into "could not parse". That verified fact is what
lets this be a cheap directory read rather than a streaming decompress-and-count.

**Noted for a later pass:** `services/media/documents.py` extracts text from PDFs page by page.
PDFs carry compressed streams too, so the same question applies there, and it was not examined in
this chunk.

## RESOLVED 2026-08-16: a 426-byte PDF could render to ~4.8 GB per page during OCR

Chunk 534, taking up the PDF question chunk 533 deferred rather than assumed away. The answer is
worse than the `.docx` case, and for a different reason: the compressed-stream question I went in
with turns out not to be the problem, and the page *geometry* is.

`services/media/documents.extract_pdf_text` OCRs a PDF that has no native text layer via
`pdf2image.convert_from_bytes(pdf_bytes, last_page=_OCR_MAX_PAGES)`. That call had a page-count
bound and no size bound, and `pdf2image` defaults to **200 DPI with `size=None`**. A page's
dimensions come from its own MediaBox, and the PDF spec allows up to 14400pt - 200 inches - a side.
200 inches at 200 DPI is 40,000 x 40,000 px: **1.6 gigapixels, ~4.8 GB as RGB, per page**, up to 25
pages.

Verified rather than argued, and without rendering it (which would have spent the memory the fix
prevents): a hand-built **426-byte** PDF declaring `/MediaBox [0 0 14400 14400]` makes the
container's own poppler report `Page size: 14400 x 14400 pts`. Nothing upstream normalises it.
`tesseract` and `pdftoppm` are both present in the app image, so the path is live, and it runs in
`process_image_upload` - a Celery task, so the blast radius is a worker rather than the web tier.

**Fixed** by passing `size=_OCR_MAX_PIXELS` (2200). Two details worth keeping:

- 2200 is a **no-op for real documents**, not a compromise. A US-Letter page is 11 inches tall,
  which at the existing 200 DPI default is exactly 2200 px - so ordinary uploads rasterise exactly
  as before and only pathological geometry is scaled.
- It is passed as a bare `int`, which `pdf2image` turns into poppler's `-scale-to` (longest side,
  aspect preserved), so one number bounds both axes whatever the page shape. A `(w, h)` tuple maps
  to `-scale-to-x`/`-scale-to-y`, which set the axes independently - that would distort, and a
  99:1 page would still blow past the intended bound on its long side.

**Also bounded: the text itself.** Both extraction paths append per page into `Image.ocr_text`, a
`TextField` with no length of its own, from an untrusted upload. Now capped at 200,000 characters
(25 dense pages is ~125 KB, so it is generous) and truncated rather than discarded, since partial
text still serves the search it was extracted for.

Tests assert against the *call* rather than the render, deliberately - what matters is that a bound
reaches poppler at all, and exercising the pathological case would spend exactly the memory in
question.

### What this did not find

The reason for looking here was compressed streams, by analogy with the `.docx` fix. That analogy
was wrong: `pypdf`'s text extraction is bounded by `_OCR_MAX_PAGES` before any stream is touched,
and the OCR path reads the PDF as opaque bytes. The compression question was a real question with a
"no" answer, and the actual defect was one the analogy would never have suggested.

## RESOLVED 2026-08-16: the import *preview* built every pin at once; the import itself does not

Chunk 535, the last item on the parser thread. The asymmetry is the finding:

- `GoogleMapsGateway.import_pins_streaming` - the actual import - is a **generator** yielding one
  SSE event per pin. It never holds the whole set, and its docstring is explicit about the
  streaming design.
- `GoogleMapsGateway.parse_for_preview` - which runs **first**, on the same files - builds every
  pin dict for every file into one list and serialises them into a single `JsonResponse`,
  in-request, with no bound anywhere in the chain.

So the path that was carefully made incremental is preceded by one that was not, on identical
input. This matters more since chunk 531 gave the archive extractor a shared 2 GB budget: "how much
can reach the parser" is now a known quantity, and "how many pin dicts that becomes" was unbounded.
Per-pin size is bounded (name 255 chars, description capped), so it is purely a count problem.

**Fixed** with `MAX_PREVIEW_PINS = 20_000`, applied across the whole upload rather than per file,
covering both the shapefile-bundle loop and the per-file loop. That is far above any hand-curated
import - it is a backstop against machine-scale files (a county parcel export) rather than a
product limit on what someone may bring in.

Reaching the cap is **reported**, not silently applied: a truncated preview otherwise looks exactly
like a smaller file. It rides the existing `warnings` array, which the preview UI already toasts.
An upload landing exactly on the boundary gets the message without having been truncated, which is
why it reads "at the preview limit" rather than "some were dropped" - a harmless over-warning
instead of a claim that might be false.

One adjacent fix that the new warning made necessary: the preview UI toasted every warning under
the hardcoded title **"Could not import a file"**, which is right for the per-file parse failures
that used to be the only occupants of that array and wrong for a notice about the preview itself.
Retitled to "Import warning", which fits both.

**Note on what remains unbounded, deliberately.** The *import* path streams per pin but still parses
each file into a list before iterating it, so a single enormous file is held in memory once during
its own import. Bounding that means changing the format parsers to be generators - a much larger
change than this one, with no in-request exposure (the import runs as a streaming response), so it
is recorded rather than attempted.

## RESOLVED 2026-08-16: posting to an archived safety check-in 500'd on the no-JS fallback

Chunk 536 stopped waiting to stumble into the session's recurring pattern - an idea applied to one
of two sibling paths, five instances by then - and hunted it directly. The sweep: for every function
defined in this codebase, compare the exception types each of its **callers** catches; report where
one caller catches strictly more than another. 83 functions diverge that way.

**Most of that is legitimate** and reading it says so: a service-internal call that lets a domain
error propagate to its own caller genuinely should not catch it, and a view that renders an error
page genuinely should. The interesting subset is where two *equivalent* surfaces - the website
controller and the external API - handle the same service call differently.

**The real one: `post_chat_message`.** Its two failures are **siblings, not parent and child** -
`SafetyValidationError` and `CheckinArchivedError` both derive from `ValueError` directly, so no
single `except` covers both. `external_api/views_safety_chat.py` catches each deliberately, with a
comment on why they differ (409 vs 400: the body was fine, the check-in's plaintext is already
sealed into its encrypted archive, so a client should retire the conversation rather than ask the
user to retype). `controllers/safety.py` caught only `SafetyValidationError`, so the archived case
escaped as a 500.

Where it lands is what makes it worth fixing rather than filing: that view is the **no-JS /
socket-down fallback** on a *safety* feature - its own comment says it exists so a message isn't
invisible when the WebSocket is down. It is the path that runs when something is already degraded.
The same controller file catches `CheckinArchivedError` correctly one method away, at line 813.

Fixed to answer 409 with the safe message, matching the API. `_chat_panel.html` surfaced only 400
bodies as sender-safe text, so it now treats 409 the same way - otherwise the user would have got
the generic "you may no longer have access to this chat" for a check-in that is merely closed.

### Verified safe by the same sweep: the pin sub-resource endpoints

The sweep's four highest-signal hits were `create_pin_alias`, `delete_pin_alias`, `create_pin_link`
and `create_pin_note` - each caught by the HTML controller and apparently by nothing on the API
side. All four dissolved on reading, and the API design is the better of the two: `PinSubResourceView.post`
and `PinSubResourceDetailView.delete` catch the shared `PinSubResourceError` base **once**, in the
base class, and map it to a status through `_subresource_error_status`, so every present and future
subclass is handled. The HTML controllers catch each concrete type individually.

`create_pin_note` looked like a genuine gap inside that - it raises a bare `ValueError`, not a
`PinSubResourceError`, so the base class would not catch it. It is unreachable from the API:
`PinNoteSerializer.text` declares `trim_whitespace=True, allow_blank=False`, so DRF answers 400
before the service function runs. Worth recording rather than "fixing" - the odd-one-out exception
type is real, and only the serializer is stopping it from mattering.

**Method note, third in a row.** The scan is intra-function, so it cannot see a handler in a base
class one frame up - which is exactly what produced its four loudest false positives. The pattern
holds: the scan points at the neighbourhood, and reading decides.

## RESOLVED 2026-08-16: a failing property test crashed the reporter and destroyed its own identity

The thirteenth consolidation (task `bl9bhhohp`, chunks 532-534) is the first non-green run in twelve.
It ended:

```
1 failed, 9074 passed, 1 xfailed, 4 warnings, 832 subtests passed in 1:34:17
```

...with **no test name anywhere in the output**, and an `INTERNALERROR` traceback instead. The run
also stopped ~1,800 tests short of the full suite.

**What happened.** When a `@given` test fails, Hypothesis' pytest plugin offers a patch adding an
`@example(...)` for the falsifying input - a convenience. Building it runs a `libcst` codemod, which
here raised `AttributeError: __provides__` inside
`libcst.matchers._visitors._gather_constructed_visit_funcs`. That runs inside
`pytest_runtest_makereport`, so it did not merely lose the suggestion: it raised while *building the
failure report*, which pytest treats as an internal error - aborting the run and taking the identity
of the failing test with it.

So the verification instrument this audit relies on was blind in precisely the situation it exists
for: it can tell you everything passed, and cannot tell you what failed.

**Not reproducible in isolation**, which is why twelve green consolidations never surfaced it: a
deliberately-failing `@given` test in a single module reports perfectly, falsifying example and all.
It needs state a long run accumulates - so the failure mode only appears in the runs whose output
matters most, and only when something has already gone wrong.

**Fixed** in `conftest.py` by making `hypothesis.extra._patching` unimportable. The plugin already
guards that import with `except ImportError: return`, so this is its own supported degradation path
rather than a monkeypatch of its internals. Verified after the change: a failing `@given` test still
reports its name, its assertion and its falsifying example; only the auto-suggested `@example`
decorator is gone. That is the whole trade, and it is worth making - a suggestion you cannot see
because the reporter crashed is worth nothing.

**The underlying failure is still unknown**, which is the point: the crash destroyed it. A fourteenth
consolidation is running to recover it, and will now be able to name it. Recorded here rather than
waiting, because "one test failed and the suite cannot say which" is itself the finding.

**Not root-caused, deliberately:** the `__provides__` collision is between `libcst`'s matcher
machinery and something a full run loads (`zope.interface`, via Twisted/Daphne, is the obvious
suspect from the attribute name - but importing those two alongside the codemod does *not* reproduce
it, so the real trigger is narrower and unidentified). Chasing a third-party interaction is not worth
it when the feature involved is optional and the fix is one line at the boundary.

### Recovering the failure the crash destroyed (chunk 538, same day)

The lost failure was located without waiting for another run, from the progress output alone.

**Method.** pytest's `-q` progress emits exactly one character per test outcome. In the thirteenth's
output the `F` sits at character 9,076, so the failing test is the 9,076th collected. That premise
was *checked, not assumed*: two known-good runs (eleventh, twelfth) have progress-character counts
of 10,889 and 10,898 against `passed + xfailed` of 10,889 and 10,898 - exact, which also establishes
that the 1,481 passing subtests emit no characters. Had subtests counted, every mapping below would
have been off by ~832.

Mapping 9,076 onto the current collection (10,917 tests, of which 7 were added after that run's
tree, both groups sorting before that point) gives ordinal 9,083:
`test_export_formats.py::test_kml_round_trips_placemark_count_and_coordinates`. Its neighbours
9,082-9,086 are all in the same file, so even a small mapping error stays inside it - and that file
is four `@given` round-trip properties, which fits: the crash only occurs for `@given` failures.

**It is not input-dependent.** All four properties were re-run at 3,000 examples each - 12,000 in
total against the default ~100 - and all pass, as does the module in isolation. So the failing input
is not rare; the failure needs something a full run accumulates. These writers are pure and never
touch the database (their own docstring says so), which points at leaked cross-module state -
locale, a monkeypatch, or an `override_settings` - the same class as the flakes recorded above.

Unresolved, and left that way rather than guessed at. The fourteenth consolidation is running and
will now be able to name it directly if it recurs.

### Correction: these runs execute as root, so the "read-only example store" mechanism does not apply

Checked while looking for a saved failing example: `docker exec` without `-u` runs as **root**, and
that is how every consolidation in this session has been invoked. The store is writable by those
runs and always was.

That undercuts the chunk-507 explanation recorded above - "the directory is owned by root and mode
755, while tests run as `appuser`, so writes fail silently" - which was the mechanism offered for
`test_only_submitted_fields_ever_move`. It is wrong for the way this audit actually runs the suite.
The `appuser` detail is true of the *application* process (per CLAUDE.local.md's `logs/` footgun);
it is not true of `docker exec`-invoked pytest.

The chunk-529 change built on that reasoning still stands on its own merits - an explicitly
registered profile and a store whose writability is proved rather than assumed is better than an
implicit default - but it did not fix a live problem, and the flake it was credited with explaining
is unexplained again. Corrected here rather than quietly, because that flake is recorded as
*resolved* on the strength of this mechanism.

### The identification corroborated, and two causes ruled out (chunk 539)

The ordinal mapping above rested on one measurement, so it got a second one from an unrelated
quantity before anyone acts on it.

**Cross-check.** The thirteenth reported **832** subtests passed; a full green run reports **1,481**.
If the abort really happened at ordinal 9,076, the missing ~649 must belong to subtest-producing
modules positioned *after* that point. There are exactly nine such modules after it
(`test_external_api_scopes`, `test_external_api_url_resolution`, `test_game_bounds_antimeridian`,
`test_html_description`, `test_longitude_wrap`, `test_map_infrastructure`, `test_place_name_meaning`,
`test_settings_env_bool`, `test_social_links`), while the large early producers - notably
`test_external_api_pin_patch_fields` at position 2,357, which is also what generates the run's 40
subTest-with-`@given` warnings - sit before it and did run. Two independent quantities now agree on
the same abort point.

Worth recording about the ordering itself: collection is deterministic but **not** plain
alphabetical. `test_export_formats.py` (module-level `def test_` functions) collects at 9,084 while
`test_export_formats_delivery.py` (a `TestCase` class) collects at 1,681 - bare functions are
grouped separately from class-based tests. A mapping that assumed alphabetical order would have
landed thousands of tests away.

**Two causes ruled out.**

- *Not input-dependent.* 3,000 examples per property, 12,000 total against the default ~100, all
  pass.
- *Not caused by the other KML/lxml modules running first.* `test_google_maps_kml_import` and
  `test_kml_import_malformed` are the only other tests touching fastkml or lxml; running both
  immediately before `test_export_formats` reproduces nothing.

What remains is the assertion the test actually makes: `geometry.x == pin.effective_longitude`,
**exact float equality** after a round-trip through KML text. That is the fragile shape worth
suspecting - it holds only while nothing in the process changes how floats are formatted or parsed -
but no mechanism has been demonstrated, and none is asserted here.

### It did not recur; the assertion stays strict (chunk 540)

The fourteenth consolidation - the first run with working failure reporting - is **green**:
10,916 passed, 1 xfailed, 0 failed, 1,481 subtests, 1:34:49. Reconciled by the corrected method:
10,916 + 1 = 10,917 collected, and chunk 539 added no tests, so it matches the current collection
exactly. The full 1,481 subtests also confirm the whole suite ran rather than aborting early as the
thirteenth did.

So the failure has occurred **once in fourteen full runs** and is not reproducible on demand. This
entry stays **open**.

**The exact float-equality assertion is deliberately kept.** Loosening
`geometry.x == pin.effective_longitude` to a tolerance is the obvious way to make a flaky test stop
flaking, and it would be wrong here: the property holds across 12,000 generated examples and 13 of
14 full suites, so it documents something that is really true, and it is the only thing that would
catch whatever caused the one failure. Weakening an assertion to silence an *unexplained* failure
converts a signal into a permanent blind spot - the same reasoning that keeps
`test_pin_detach_location` a strict xfail rather than an assertion of the current 500.

What did change is diagnosability. Both assertions now carry a message printing each value's `repr`
**and** `float.hex()`, so a recurrence is actionable straight from the run output: a one-ulp
difference is invisible in decimal repr and obvious in hex. If it returns, the next reader gets the
actual values instead of `assert 1.0 == 1.0`.

## RESOLVED 2026-08-16: the account-deletion reminder could email twice; every sibling sweep was already locked

Chunk 541 opened a thread on beat-task idempotency, since Celery delivers at least once and a sweep
that outruns its own interval overlaps itself.

This codebase already has the answer: `services/core/locks.acquire_lock`, a cache-based overlap lock
whose docstring even prescribes the TTL ("just under the task's beat interval, so a tick is never
skipped"). An AST sweep of all **24** beat-scheduled tasks shows 10 take it and 14 do not.

**The 14 are almost all correct.** Reading them rather than reporting them: `prune_*`,
`hard_delete_expired_*`, `delete_expired_safety_checkins` and `cleanup_vestigial_assets_task` delete
rows, so a second run finds nothing; `upgrade_placeholder_pin_names` and `sweep_achievements`
recompute to the same result; `sync_stripe_subscriptions` reconciles from Stripe. The one that
looked most dangerous - `advance_pwyw_usage_ledgers`, which moves billing state - is idempotent *by
construction*: it walks forward from `usage_covered_until` and stops as soon as the next period has
not started, so a repeat run advances nothing and returns before writing, and two concurrent runs
starting from the same cursor compute the same target rather than stacking.

**One is a real gap: `send_account_deletion_reminders`.** It is the only unlocked beat task whose
repetition is *visible to a user*. `due_for_deletion_reminder` filters on
`deletion_reminder_sent_at__isnull=True` - a selection-time guard - and `send_deletion_reminder`
creates the notification, sends the email, and only then stamps the marker. Two overlapping runs both
select the same profile and both send, so the user gets two "your account will be deleted tomorrow"
notices. Its own docstring claims "Idempotent via `deletion_reminder_sent_at`", which is the same
false-confidence shape recorded for `FriendInvitation.mark_accepted`.

Its three sibling reminder sweeps - `send_due_checkin_reminders`, `send_final_checkin_warnings`,
`escalate_overdue_checkins` - all take the lock. The convention was applied everywhere it was needed
except here.

**Fixed with the lock, not with a claim-before-send**, and the distinction is the point. The
claim-first fix used for `FriendInvitation.mark_accepted` is wrong for this task because the failure
directions are not symmetric: a duplicate is a second warning email, while a lost one is *no* warning
before a permanent account deletion. A lock loses nothing - the skipped run leaves the marker unset,
so the next tick sends it - which the regression test asserts directly.

### Verified safe: every overlap lock's TTL obeys its own rule (chunk 542)

**CORRECTED 2026-08-16 (chunk 544): a test already enforced this, and I did not look.** The claim
below that "nothing checks it" is false. `test_beat_lock_intervals.py` - written earlier in this
same session (`0d4f87ae`) - already asserts the TTL-versus-interval invariant, in both directions,
*and* carries a completeness arm that fails when a lock-guarded beat task is missing from its map.
It is strictly better than the guard chunk 543 then went and wrote on the strength of this false
premise: it matches both lock idioms (`cache.add` and `acquire_lock`/`beat_lock`) where mine matched
one, and it has the completeness check mine lacked entirely.

That duplicate guard has been deleted. The measurements below stand - all eleven TTLs do sit at
90-92% of their interval - but they were a re-derivation of something already enforced, not a new
finding.

`acquire_lock`'s docstring states the constraint on the TTL callers pass it - "should sit just under
the task's beat interval, so a tick is never skipped by a lock the previous run has already finished
with". A convention that is stated is worth checking, because the failure is invisible either way:
a TTL **above** the interval means a run killed mid-flight blocks the next tick (and for the safety
sweeps, that is a missed escalation); a TTL far **below** the true runtime means the lock expires
mid-run and the overlap it exists to prevent happens anyway.

All eleven locked beat tasks obey it, at 90-92% of their interval:

| task(s) | interval | TTL |
| --- | --- | --- |
| three stall sweeps (spotguessr / trivia / consensus) | 120s | 110s |
| four safety sweeps (due reminders, final warnings, escalation, archival) | 300s | 270s |
| enrichment, trivia generation, trivia wiki incorporation, account-deletion reminders | 3600s | 3300s |

Nothing to change. Recorded because the numbers are spread across two files - the TTL constants in
`tasks.py`, the intervals in `settings/base.py` - so the invariant is only checkable by putting them
side by side, and nothing does that automatically. A task whose schedule is retuned without its lock
constant would break this silently.

That closes the beat-task thread: 24 scheduled tasks, 11 correctly locked, 13 idempotent by
construction, one gap found and fixed (the account-deletion reminder). Twenty-second verified-safe
area.

### Enforced 2026-08-16 (chunk 543): the beat-lock TTL invariant now fails the build

The note above ends "nothing checks it, and retuning a schedule without its lock constant would
break this silently". `test_beat_lock_ttl_guard.py` now does, following the
`test_bulk_write_signal_guard.py` precedent.

It asserts both directions - no TTL at or above its interval (a killed run would skip the next
tick), and none below half of it (the lock would lapse mid-run and prevent nothing) - and reads both
sides **live**: the TTL constants off the imported `tasks` module and the intervals off Django's own
`CELERY_BEAT_SCHEDULE`, so it checks the values the workers actually run with. Only the
task-to-lock-constant mapping comes from the AST, because that association exists nowhere else.

Three details worth keeping:

- **It was verified to fail.** Raising `_CHECKIN_LOCK_TIMEOUT_SECONDS` to 600s against its 300s
  interval, and separately dropping it to 10s, each produce a named offender. A guard nobody has
  seen fail is a guard nobody knows works.
- **Its own guard-the-guard test caught a bug in it.** The first version derived crontab intervals
  from `remaining_estimate`, which answers "how long until the next fire, *from now*" rather than
  from its argument - so calling it twice compounded instead of stepping, and produced *negative*
  intervals. The `all(seconds > 0)` assertion failed immediately. Without that check the guard would
  have compared every TTL against a negative number, passed, and guarded nothing. This is the exact
  failure the bulk-write guard's docstring warns about, arriving on schedule.
- Intervals are now derived from the crontab's field cardinalities, which is exact for every regular
  pattern and returns `None` otherwise - and an unsupported pattern surfaces, because the
  guard-the-guard test requires at least 20 of the 24 to resolve.

## RESOLVED 2026-08-16: chunk 541's new lock broke the guard that enumerates locked beat tasks

The fifteenth consolidation is the first run to name its own failure since the reporter fix, and
what it named was mine:

```
FAILED test_beat_lock_intervals.py::BeatLockIntervalTests::test_every_beat_scheduled_task_that_takes_a_lock_is_covered
1 failed, 10915 passed, 1 xfailed, 1481 subtests passed
```

Chunk 541 gave `send_account_deletion_reminders` an overlap lock and did not add it to
`_LOCKED_BEAT_TASKS`. That map is what `test_beat_lock_intervals.py`'s completeness arm checks, and
its docstring states exactly why the arm exists: "a new lock-guarded beat task must be added to the
map below or it fails here, rather than being silently skipped by a test that only knows about the
tasks someone remembered."

So the guard worked precisely as designed, on the first new lock added after it was written. Fixed
by adding the entry.

**The larger correction is that chunks 542 and 543 should never have happened as they did.** Chunk
542 measured the TTL invariant by hand and concluded "nothing checks it"; chunk 543 built
`test_beat_lock_ttl_guard.py` to enforce it. Both rested on a premise I never verified - that no
such test existed - when one written earlier *in this same session* did, and did it better. The
duplicate is deleted.

Two things worth taking from it. First, chunk 543's own recorded lesson was "copying the guard was
worth less than copying the paranoia that came with it" - and the paranoia I failed to apply was to
my own claim that no guard existed. Second, this is the third correction to my own recorded
reasoning (after chunk 532's arithmetic and chunk 538's root/appuser premise), and all three share a
shape: a claim stated once, then built on, without the check that would have cost a single grep.

## RESOLVED 2026-08-16: a completeness guard whose completeness arm pointed the wrong way

Chunk 545 audited the auditors: this codebase has thirteen guard/coverage tests, and chunk 544 had
just shown one of them catching a real regression. The question was whether the rest still *bind*.

Most do, and the survey is worth recording so it is not redone: `test_pin_cycle_guard` and
`test_wiki_cycle_guard` are behavioural rather than scan-based (no vacuous-pass risk);
`test_export_import_completeness` is 45 explicit per-field assertions rather than a derived
population; the scan-based ones - bulk-write signals, external-API scopes, journal-source scopes,
label-merge relations, plugin rate limits, settings round-trip, undo scopes - each carry a
"the scan still finds something" assertion.

**One is wrong.** `test_undo_photo_reattachment_coverage`'s completeness arm asserts

```python
set(_PHOTO_OWNERS).issubset(actual_SET_NULL_owners_of_Image)
```

which catches a *stale* entry - the list naming a relation `Image` no longer has - and permits
exactly what the test's own docstring promises to prevent: "a fourth owner must not repeat this
silently." A new `SET_NULL` photo owner arriving with an undo handler leaves `_PHOTO_OWNERS` a
subset, so the guard passes while that owner's photos go unrestored on undo - the original bug,
repeated silently, by the test written to stop it.

**Not currently live.** `Image` has seven `SET_NULL` owners (`pin`, `wiki`, `safetycheckin`,
`location`, `pinvisit`, `pinsuggestion`, `directmessage`) and only the first three have undo
handlers, all three listed. The direction is latently wrong, not presently wrong.

Fixed by asserting both directions: the existing subset (catches a stale entry) plus its converse
restricted to owners that actually have a handler - owners without one are genuinely out of scope,
since nothing restores what nothing undoes. Verified to bind by removing `wiki` from the map and
watching the new assertion name it.

That makes four corrections in this stretch where the defect was in reasoning rather than in
product code - three of them mine, this one inherited - and all four share the shape: an assertion
or claim that reads as if it establishes something it does not.

### Verified safe 2026-08-16 (chunk 546): the guards' allowlists and thresholds are not stale

Following chunk 545's one defective guard, this pass checked the other failure mode: an allowlist
that has quietly grown to swallow the population it was meant to constrain, or a threshold no longer
binding.

Five files matched a grep for allowlist-shaped names; **two were false positives** -
`test_map_controller.py`'s `_GEOLOCATION_TRACKING_ALLOWED` is a template variable, and
`test_route_query_scaling.py`'s `_ALLOWED_GROWTH = 2` is a per-route query-growth threshold rather
than a list of exempted routes. The three genuine allowlists are all small and justified:

- `test_cross_user_route_access.py::_ALLOWED_200` - **one** entry (`trips.child_trip_search`), with
  a paragraph explaining why a stranger legitimately gets 200 from it. The sweep asserts it still
  finds >100 routes and >10 nested routes, so it cannot pass vacuously.
- `test_bulk_write_signal_guard.py::REVIEWED` - each entry carries its reasoning; the guard only
  ever catches *new* bulk writes, which is its stated design.
- `test_migration_noop_reverse_guard.py::REVIEWED` - added in chunk 544, with a per-file reason.

No changes warranted.

**A correction to chunk 545's framing.** That entry proposed as a new standard: "an assertion is a
claim exactly as much as a count is; break it on purpose and watch it fail, or it is decoration."
This codebase had already written that down and practised it. `test_route_query_scaling.py`'s
docstring records that **two earlier versions of that sweep reported "all routes flat" while being
structurally incapable of seeing the one N+1 known to exist**, and that the current version was
trusted only after reverting a fix and watching `label.rows` light up at +80 queries - concluding
"a scaling sweep that has never been shown to catch anything is indistinguishable from one that
cannot". The rule is the codebase's, not mine; I restated it as though introducing it.

That makes a fifth instance of the same shape as the four corrections above - a claim that reads as
establishing more than it does. It is worth counting because the pattern is consistent: the errors
in this audit have clustered almost entirely in what I have asserted *about* the work, not in the
work.

## RESOLVED 2026-08-16: the calendar importer's trip invite named a user the app masks

Fourth instance of the identity-masking class, found by asking whether the fix from the third
("Reply/reaction notifications named people the thread masks", 2026-08-07) had reached every
sibling. It had not.

Two functions create the identical `ADDED_TO_TRIP` notification:

- `services/trips/trip_membership.invite_to_trip` resolves the actor first -
  `resolve_visible_identity(invitee, inviter)["display_name"]` - with a comment explaining that the
  message is stored as plain text and must therefore be masked at write time, not at render time.
- `services/trips/calendar_sync._invite_participants`, which invites everyone matched from an
  imported Google Calendar event, formatted `f'{importer.username} added you to the trip ...'`
  straight from the raw username, and omitted `source_profile` as well.

**Being friends is not the permission being checked.** That path only invites friends, which looks
like it makes masking moot - and does not. `VisibilityChoice`'s own docstring says accepted friends
qualify for every level **except `NO_ONE`**, so an importer who has set their profile to "No one"
is masked everywhere in the app and was named here. The same function's "not friends" diagnostic
(`f"{invitee.username} was not invited..."`) named the *invitee* back to the importer with the same
problem, from a list that may itself have shown them a placeholder.

Severity is the reason this class keeps being worth chasing: a `NotificationLog` insert is picked up
by `enqueue_native_push` and by `notification_text_alerts`, which builds an SMS body from the stored
text. The unmasked name leaves the app, to a device, and cannot be recalled by fixing a template.

Both sites now resolve through `resolve_visible_identity`, and the notification records
`source_profile` like its sibling. Covered by `CalendarInviteIdentityMaskingTests`, including an
anti-vacuity test that an ordinary friend is still named.

**Method note.** The scan that found it listed all 39 `NotificationLog.objects.create` sites and
flagged the 16 interpolating a name-shaped attribute; the candidate stood out only because a sibling
call two files away did the same thing correctly. Reading the 16 was necessary - the other 15 are
fine, several because the actor is someone the recipient has just interacted with directly.

### Verified safe 2026-08-16 (chunk 548): the off-app surfaces inherit the masking rather than bypassing it

Chunk 547 fixed a raw username in a `NotificationLog` message. The obvious next question is whether
the other channels that carry text off the device - email, native push, SMS - name people
independently, since fixing the notification would not help if they did.

They do not, and the reason is the design decision the earlier fix's comment states: masking happens
at **write** time, into the stored text, not at render time. Everything downstream inherits it.

- **Push**: `enqueue_native_push` forwards only the notification's **pk**; `dispatch_native_push`
  reloads the row and calls `as_push_payload(notification)`. The payload is the stored title and
  message, so a masked write is a masked push. `push.py` interpolates no profile fields of its own.
- **SMS**: `notification_text_alerts` builds its body from `notification.title` - same inheritance.
- **Email templates**: of sixteen, only three render a name at all, and each is correct.
  `new_direct_message.html` and `account_deletion_reminder.html` greet the **recipient** by their own
  username; `friend_invite.html` names the **inviter** to someone they are personally inviting to the
  app, who is not yet a user and has no visibility relationship to apply.

**One deliberate exception, confirmed rather than assumed.** `safety_checkin_wiki.html` renders
`{{ checkin.profile.username }}` to every profile with a pin at the destination - strangers. That is
`post_checkin_to_community_wiki`, which its docstring says runs "only when the owner opted in
(``checkin.notify_community_wiki``)". It is a rescue request: "someone near you has not checked in"
is useless anonymised, and the owner chose this disclosure explicitly. Named on purpose, not leaked.

The transferable point is that write-time masking is what makes this checkable at all. Had the
earlier fix masked at render time, each of the three channels would need its own correct
implementation and its own test, and this sweep would have had three chances to find a gap instead
of one place to confirm.

### Verified safe 2026-08-16 (chunk 549): cache keys scope what they need to

Swept a property with real leak potential and no prior pass: **does any cache key holding per-user
data omit the user?** A missing profile id in a shared key serves one account's data to another.

142 Django-cache calls; 126 have key expressions naming no user. Reading them - the count was never
the finding - shows three correct patterns rather than a gap:

- **Per-user data carries the profile in the key.** `MapPinCache` prefixes every key
  `ul:map-pins:{VERSION}:profile:{profile_id}`, and the version means a payload-shape change cannot
  serve stale entries either.
- **Shared data is keyed by what produced it, including the settings that change it.** The
  nearby-places cache (`controllers/maps.py`) keys on rounded coordinates, radius **and** a
  `source_key` encoding which providers the requester has enabled - so a user with Google disabled
  is never served a Google-sourced entry. The infrastructure-map cache keys on a rounded bbox, which
  is right: Overpass data is public and identical for everyone.
- **An unguessable id plus an ownership check in the value.** The export job status is keyed
  `dashboard:export:{job_id}:status` and stores `user_id`; `ExportStatusView` validates the id is a
  UUID, then refuses when `data.get("user_id") != request.user.pk`. The key alone is not the
  authorization - the check is.

The rest are genuinely global by nature: beat-task locks, sweep cursors, external-API session
tokens, provider-down markers, infra stats.

No changes warranted. Recorded mainly for the next person adding a cache: the rule this codebase
follows is *the key must contain everything that changes the value* - which is why the provider
flags are in the places key and why the profile is in the map-pins key, and why neither needed a
user check at read time while the export status did.

### Verified safe 2026-08-16 (chunk 550): no API serializer writes past its column

Applied the divergence lens to a fresh pair - **serializer bounds versus the model column they
write**. An unbounded serializer field feeding a bounded column is a 500 where a 400 belongs, since
Postgres raises on `varchar` overflow and nothing calls `full_clean()` on these paths.

19 writable `CharField`s across the external-API serializers declare no `max_length`. None is a
defect:

- Most write nothing - cursors, bboxes, `geo_bounds`, `sources` are query parameters.
- The rest target unbounded `TextField`s (`Label.description`, `Label.keywords`,
  `CustomFieldValue`'s value column, message ciphertext).
- The two that *do* reach a bounded column are validated before the write:
  `LabelBulkEditSerializer.color` goes through `clean_color`, which can only return a hex string or
  the default, and `PinBulkEditSerializer.description` is length-checked against
  `MAX_PIN_DESCRIPTION_LENGTH` by the same `text_length_error` call the website's bulk edit uses.

**A refinement to the divergence lens.** `AvatarEmojiSerializer` documents an API/site divergence
that is deliberate: the site's picker silently substitutes a default for an unrecognised colour,
while the API refuses, because "an API client that sent `purpel` should be told, not handed a grey
fox and left to wonder". Its colour is constrained to a closed set rather than a length - stronger
than the bound this sweep was looking for, and invisible to a scan for `max_length`.

Two lessons for the next pass with this lens: divergence between two surfaces is not automatically a
defect, and this codebase marks the intentional ones in the docstring; and a field can be *more*
constrained than the property being swept, so absence of the thing you are scanning for is not
absence of validation.

## RESOLVED 2026-08-16: an object with a legal-length name could be created but never deleted

Found by widening the write-route smoke sweep (chunk 553). Measuring first, the parameters blocking
the most routes were `session_id` (26), `profile_slug` (19), `group_uuid` (12), `profile_id` (12) and
`label_kind` (11); the last four are nearly free to supply, and adding them - plus support for
multi-parameter routes where *every* parameter is known - widened the sweep by ~60 routes.

It immediately found `label.delete` raising `DataError: value too long for type character
varying(255)`.

**The chain.** `stash_for_undo` writes `handler.describe(instances)` into `UndoAction.object_repr`,
a `CharField(255)`, untruncated. `LabelUndoHandler.describe` embeds the label's name in fixed
surrounding text - and `Label.name` is **itself** `max_length=255`. So a label named at its own legal
maximum produces a description longer than the column that stores it, and the insert fails.

The user-visible behaviour is the worst part: the object is created without complaint, and *delete*
is what 500s. You can make it and then never remove it. `Pin.name` is also 255, so every model whose
deletion funnels through this call shares the exposure, and the bulk paths (which describe several
names at once) overflow far sooner.

**Fixed at the chokepoint** - `stash_for_undo` truncates to the column's own `max_length`, read off
the field rather than written as a literal so the two cannot drift. Every handler inherits it,
including ones added later.

Covered by `UndoDescriptionFitsItsColumnTests`, including an anti-vacuity test asserting the
un-truncated description really does exceed the column (otherwise the fix would be untested) and one
for the bulk path.

**A note on the instrument.** Widening the sweep first produced a cascade of
`TransactionManagementError`s that hid the real cause: a `TestCase` runs in one transaction, so the
first route to raise a database error poisons it and every subsequent request fails. Fixed by giving
each request its own savepoint - the same fix chunk 526 applied to `pin_merge`'s recovery paths, met
this time in the test harness rather than the product. Without it the sweep reported the cascade and
named the wrong route.

## RESOLVED 2026-08-16: GET on `pin.link.to` was a guaranteed 500 (and six crashes that were my fixture)

Chunk 556 added **GET** to the route smoke sweep. Its own comment had claimed GET was "already
covered by the cross-user sweep" - which is false in exactly the way that justified building this
file: that sweep flags only `200`, so a GET answering 500 passes it silently. The same over-claim,
written by me, in the file arguing against it.

**The real finding: `PinRelinkView.get` did not accept `location_slug`.** The view backs two routes
(`pin.link` and `pin.link/<location_slug>/`), and `post()` correctly declares
`location_slug=None` - but `get()` omitted the parameter entirely, so any GET to `pin.link.to`
raised `TypeError` before a line of application code ran. Reachable by anyone who edits a URL.

This is the third instance of one shape: **one view, two routes, a signature that fits only one of
them** - after `saved_filters.new` (chunk 552) and, on the POST side of this very view, the filed
detach-location decision. GET on `pin.link.to` has nothing to choose (the location is already named),
so it now answers 405 rather than rendering a picker for a decision already made.

### Six crashes that were the fixture, not the code

The same run reported `ValueError: The 'image' attribute has no file associated with it` from six
views (`home.view`, `memories.photos`, `pin.gallery`, `comments.image_picker`, ...). That was
**mine**: `baker.make("dashboard.Image", ...)` creates a row with no file, and `Image.image` is
`null=False, blank=False` - a state the model forbids and no upload path produces. Hardening six
views against it would have been defending against the test.

Recorded rather than quietly fixed, because the distinction is the whole discipline of this sweep: a
generic instrument produces states the application cannot, and every crash it reports has to be
checked against whether a user could reach it. The fixture now attaches a real file, which is what
every upload path leaves behind.

## RESOLVED 2026-08-18: blocking leaves a saved emergency-contact default pointing at the blocked profile

**Resolution: the third option this filing proposed - keep the row, and say so.**

Neither silent answer is chosen for the owner, because both are wrong in an obvious way: leaving it
pages someone they blocked, and deleting it destroys a safety contact in the one feature whose
entire purpose is that somebody is told when you do not come back. Someone may block a person
socially and still want them called if they go missing.

``services.visits.safety.blocked_default_contacts`` reports which saved defaults now resolve to a
blocked profile (in both directions, via ``Profile.are_blocked``), and the check-in creation form
and the safety settings page both warn, naming them, and pointing at where to remove them. The row
is untouched.

Covered by ``test_safety_blocked_contact_warning.py``, including that blocking does *not* delete
the default, that it holds whichever side placed the block, and that an email-only default has no
profile to check.

The original filing follows.

## RESOLVED 2026-08-18: importing buildings on a *pin* page 500'd on the wiki side

Reported from staging with a traceback: adding several buildings from the pin detail page raised
`ChildWikiLocationError: There is already a wiki marker at these exact coordinates` out of
`mirror_buildings_to_wiki` -> `_location_for_child_wiki`, **after** the child pins had already been
created. The user saw a 500 for work that had succeeded.

Three defects behind the one traceback, all fixed:

- A building whose coordinate coincides with an existing wiki marker raised instead of being
  skipped. The common case is the parent wiki itself, because a parcel's coordinate is frequently
  one of its buildings' centroids - so the building is already represented and skipping it is the
  right answer. One such building used to abort the whole mirror.
- The mirror ran inline in the request. It is now a task (`tasks.mirror_buildings_to_wiki`), taking
  selection keys rather than records so a stale key simply resolves to nothing. Both import paths
  (the panel action and the restructure apply) enqueue it.
- The mirror did nothing when the place had no wiki, so the community side never gained the
  buildings. It now seeds a *draft* - the same thing `ensure_draft_wiki_for_location` already
  creates for every pinned location, invisible until claimed - which keeps "community pages are
  promoted explicitly, never created official behind a user's back" intact.

Note for anyone testing this area: `CELERY_TASK_ALWAYS_EAGER` is opt-in via
`UL_CELERY_TASK_ALWAYS_EAGER` and is **off** in the normal test settings, so an enqueued task does
not run during a test. Three existing tests asserted child wikis appeared after a POST; they now
exercise the mirror directly and assert separately that the view enqueues it.

## ~~OPEN 2026-08-12: trip activity weather matches against times in the wrong timezone~~ RESOLVED 2026-08-15 (`f3acdf56`)

**RESOLVED without a timezone library.** This entry framed the fix as needing a per-location
timezone (and therefore a product decision); it does not. Open-Meteo's `timezone=auto` response
already carries a top-level `utc_offset_seconds`, which is enough to recover the real instant.
`ForecastSlot` now has an aware-UTC `date_utc` populated by all three converters (Open-Meteo via
that offset, OpenWeatherMap from its UTC `dt_txt`, REData from its parsed value - documented as
the TypedDict's contract), and `_build_activity_forecasts` matches slots and computes `gap_hours`
against it, with the old naive comparison kept as a fallback for slots lacking it. The AI
suggestion day-bucketing got the same correction. Display still uses the naive local `date`, so
no panel changed. The crash half was already fixed earlier. Original entry below.

`ForecastSlot.date` has no timezone contract, and the three providers that populate it disagree.
`controllers/trip.py::_build_activity_forecasts` then compares them against an activity's
scheduled time:

```python
target = act.scheduled_at              # aware, stored UTC
if target.tzinfo is not None:
    target = target.replace(tzinfo=None)   # -> naive *UTC wall clock*
closest = min(slots, key=lambda s: abs((s["date"] - target).total_seconds()))
```

The provider chain in `weather_resolution.get_raw_forecast_slots` is REData → OpenWeatherMap →
**Open-Meteo**, and Open-Meteo is the unconditional final fallback (no API key required, so it is
the live path for any install without OWM/REData configured). `OpenMeteoGateway` requests
`"timezone": "auto"` and its own docstring says that "resolves the correct local timezone for the
coordinates server-side" - so its `starts_at` strings are **naive local time for the pin's
location**, while `target` is naive **UTC**.

So on that path the subtraction is local-minus-UTC: out by the location's offset - 4-5 hours in
New York, 9 in Tokyo, 12-13 in Auckland. Two visible effects: the "closest" slot can be the wrong
one (a user sees the wrong weather for their activity), and the `gap_hours > 36` out-of-range test
is skewed by the same amount, so activities near that boundary are misclassified.

The other two providers differ again: OpenWeatherMap's `dt_txt` is UTC (so that path is correct by
accident), and REData's format is whatever its API emits - `datetime.fromisoformat` passes the
awareness straight through, so if REData ever returns an offset the slots become *aware* and the
subtraction raises `TypeError: can't subtract offset-naive and offset-aware datetimes`.

**Partly addressed 2026-08-12 - the crash, not the offset.** The mixed-awareness subtraction is now
guarded: both sides are forced naive before comparing, so a provider that emits an offset can no
longer 500 the trip page with "can't subtract offset-naive and offset-aware datetimes". Confirmed
the guard is load-bearing by reverting it and watching the TypeError return, and covered by
`test_trip_forecast_mixed_awareness.py` - which deliberately does *not* assert that the correct
slot is chosen on the Open-Meteo path, because it still isn't.

The offset bug below is untouched and remains the substance of this entry. It was re-checked while
fixing the crash: the app has no timezone-resolution library and no per-location timezone field, so
a correct comparison genuinely cannot be built from what is already here.

**Not fixed here because the right fix is a product decision.** Normalising everything to UTC is
the obvious engineering answer, but `timezone=auto` is presumably deliberate: the pin weather
panels want to *display* local time, and switching the provider request to UTC would change what
users see everywhere, not just in trip matching. A correct fix keeps local for display and makes
the comparison timezone-aware (which needs the location's timezone), or gives `ForecastSlot` an
explicit documented contract - either way it wants an owner's call.

Found by chasing the single `RuntimeWarning` in a full test run (a naive datetime reaching
`Pin.last_visited`). That warning itself is only test-fixture hygiene -
`test_pin_queryset.py:134` passes `date.today()` - but it prompted the sweep that turned this up.

## ~~OPEN 2026-08-11: bulk-accepting pin suggestions makes up to 200 live API calls inside one request~~ RESOLVED 2026-08-15 (`683f0632`)

**RESOLVED**: the product blocker this entry named ("do we accept placeholder names?") had already
dissolved - lazy name resolution exists end to end (`resolve_location_place_name` backfills in the
background, and `place_info.py`'s own docstring blesses `fetch_if_missing=False` for bulk paths).
So `fetch_if_missing` is now threaded through `_create_location_with_canonical_name` →
`resolve_location_for_point` → `accept_pin_suggestion`; both bulk views pass `False` and enqueue
the backfill per newly created Location, gated on `external_apis_enabled`. **No user-visible
naming change**: `pin.name` comes from the suggestion regardless, so no placeholder ever appears -
only the shared Location's official name backfills asynchronously. Single-accept stays
synchronous. Tests assert the Google entry point is never called from either bulk endpoint.
Original entry below.

Found by root-causing the last failing test in the suite (`test_pin_suggestion_bulk_partial::
test_accepting_marks_the_suggestions_handled`, which was reaching the real internet). The test is
fixed; the behaviour it exposed is not, and is a product issue rather than a test one.

Accepting a pin suggestion resolves the new place's canonical name **synchronously, inside the
request**:

```
PinSuggestionBulkActionView.post           controllers/pin_suggestions.py:259
  accept_pin_suggestion                    services/pins/pin_suggestions.py:865
    resolve_location_for_point             services/visits/visits.py:194
      _create_location_with_canonical_name controllers/maps.py:1140
        GooglePlaceService._resolve_name   .../google/place_info.py:219
          resolve_name_from_nearby         .../places_resolution.py:334
            RedataPlacesGateway.search_nearby  → outbound HTTP
```

The controller loops over the submitted ids and calls that per suggestion, and
`_MAX_BULK_SUGGESTIONS = 200`. A suggestion whose coordinates already have a `Location` skips the
lookup, so the cost is per *new* place - but a bulk accept of suggestions at 200 distinct places
issues 200 sequential outbound requests in one request/response cycle. The rate limiter
additionally serialises them: `_reserve_call` takes `select_for_update()` on the service's
`ApiRateLimit` row for each call.

Timeout budget makes the tail bad: the shared gateway wrapper defaults to `(5, 30)` connect/read
seconds (`rate_limiter.py:626`). Even a modest 300ms per call is ~a minute wall-clock; a slow
provider is unbounded in practice. nginx will cut the connection long before that, leaving the
user an error on work that partially committed, with a gunicorn worker pinned throughout.

This is exactly the case `CLAUDE.md` already calls out as roadmap work - "keep moving remaining
slow operations (API calls, geocoding, import jobs) onto Celery; all non-instant UI operations
must show a progress indicator". **Not fixed here because it needs a product decision**, not just
a refactor: deferring name resolution means the pin is created with a placeholder name and
renamed a moment later, which is visible to the user and interacts with
`name_is_user_provided`.

### Scope, measured - the bulk loop is the urgent part, not the pattern

Instrumented the gateway chokepoint (`_RateLimitedSession._do_request`) plus raw
`requests.Session.request` and walked 15 ordinary GET endpoints (map, trips, memories, profile,
wiki, pin json, ...): **0 of 15 attempted an outbound call synchronously**. Page rendering is
clean - panel data goes through Celery (`schedule_panel_fetch`). So this is not a systemic
"requests call APIs inline" problem.

It is confined to write paths that may need to *create a Location*, all of which reach
`resolve_location_for_point` / `_create_location_with_canonical_name`:

| caller | calls per user action |
|---|---|
| `controllers/pin_edit.py:639` (move/edit a pin) | 1 |
| `services/memories/photos.py:221` (create pin from photo) | 1 |
| `services/visits/visits.py:213` (log a visit) | 1 |
| `services/pins/pin_suggestions.py:865` via the **bulk** endpoint | **up to 200** |

The single-call sites cost one lookup per action and are ordinary roadmap work. The bulk endpoint
is the one that turns a bounded cost into an unbounded one, and is worth addressing on its own
even before the broader Celery migration. (13 test modules already mock
`GooglePlaceService._resolve_name`, which is a good independent map of everything on this path.)

## ~~PARTLY RESOLVED 2026-08-12: the nightly achievement sweep is O(profiles × metrics) and gets killed at 3600s~~ RESOLVED 2026-08-15 (`5ac09566`)

**RESOLVED - both remaining halves.** (2) `sweep_achievements` is now a dispatcher that slices
profiles into pk ranges and enqueues a per-chunk subtask, so no single invocation can approach the
3600s `CELERY_TASK_TIME_LIMIT` and a crashed chunk affects only its own range. (1) `Metric` gained
an optional `compute_bulk` implemented via grouped aggregates for the count-shaped metrics, with
`compute_values_bulk` falling back to per-profile `value_for` where a grouped aggregate is not
equivalent - the streak metrics stay per-profile deliberately, since they are path-dependent.
Property tests assert `compute_bulk` agrees with per-profile `compute`. The resume/checkpoint
cursor from the earlier pass is retained. Original entry below.

`tasks.sweep_achievements` → `evaluate_all_profiles` iterates **every** `Profile` and evaluates
every active achievement for each one. Each metric is an independent per-profile query -
`_pins_created` is literally `Pin.objects.filter(profile=profile).count()`, and the other 18 are
the same shape.

Measured (19 active achievements, one per registered metric):

```
   4 profiles ->   126 queries  (31.5 per profile)
  16 profiles ->   492 queries  (30.8 per profile)
  marginal cost: 30.5 queries per additional profile
```

So the sweep costs ~30 queries per user per night, with no batching. At 10k users that is ~300k
queries per run; at 100k users, ~3M.

**The failure mode is worse than "slow".** `CELERY_TASK_TIME_LIMIT` is a hard 3600s
(`settings/base.py:245`) and the whole sweep is one task, so once the run exceeds an hour the
worker is killed mid-iteration. `Profile.objects.iterator()` has a stable order, so it is always
*the same tail* of profiles that never gets evaluated - and nothing reports it, because the task
simply dies. Those users silently stop earning the awards that only this safety net catches
(thresholds crossed by time passing rather than by a write, per the task's own docstring).

Two independent fixes, either of which helps:

1. **Batch the metrics.** Give `Metric` a bulk variant so each one is a single grouped aggregate
   across all profiles (`Pin.objects.values("profile").annotate(n=Count("id"))`) instead of a
   query per profile. That turns 30×N into ~19 queries plus in-memory comparisons. This is the
   real fix, but it means touching the metric protocol and all 19 implementations.
2. **Chunk the task.** Split the sweep over profile-id ranges dispatched as separate tasks, so no
   single invocation can be killed mid-way and silently drop a fixed tail. Much smaller change,
   and it removes the *silent* part of the failure even without (1).

**Update 2026-08-12 - the silent part is fixed; the cost is not.** Neither (1) nor (2) was
attempted, but a third, much smaller change removes the part that actually harms users. The sweep
now checkpoints its progress to the cache every 500 profiles and resumes from there, resetting the
cursor once it reaches the end. A killed run therefore no longer truncates at the *same* place
every night: whatever a resumed run skips is covered by the following one, so no profile can be
starved of awards indefinitely. A resumed run also logs a warning, which is what makes the
truncation visible at all - previously the task simply died and nothing said so.

This needed no decision about batch size or task shape, which is why it was safe to do unattended.
It does **not** reduce the ~30 queries per profile: fix (1) is still the real answer, and (2) is
still worth doing if the run time keeps growing.

Not attempted here: (1) is a refactor across the metric registry, and (2) changes the shape of a
scheduled job - both want a maintainer's call on batch size and ordering guarantees.

## ~~OPEN 2026-07-26: FCM push transport is registered but never dispatched~~ RESOLVED 2026-08-15 (`60c6f6cb`, tier 1 - honesty, not dispatch)

**RESOLVED for the harm actually recorded here** (a registrant getting silence with no signal):
`PushDevice.dispatch_enabled` (true only for UnifiedPush) is now surfaced read-only on
`PushDeviceResponseSerializer`, so both the register 201 and any future list response say plainly
whether the server will ever push to that device, and `docs/EXTERNAL_API.md` documents the field
plus the FCM caveat. FCM registrations are still **accepted deliberately** - rejecting them would
break the documented contract and the module keeps them so re-registration is seamless once a
sender exists. Actual FCM dispatch (HTTP v1, service-account credential, google-auth dependency)
remains unbuilt and is correctly gated on the mobile client existing. Original entry below.

`services/notifications/push.py` accepts and stores FCM device registrations, but only the UnifiedPush
transport actually dispatches; FCM rows are skipped at send time until a Play-flavor client
exists (see that module's docstring). This is server-side dispatch infrastructure requiring a
Google service-account credential - it is *not* a missing external-API endpoint, and
`push-devices/` already registers such devices correctly. Recorded here because the gap was
previously documented only in a module docstring, so a user registering an FCM device today
gets silence rather than an error.

## ~~OPEN 2026-08-12: which setting owns dwell-detected visits?~~ RESOLVED 2026-08-15: both gate

**RESOLVED - the codebase already answered the product question.** The sibling Takeout importers
(`google/location_history.py`, `google/my_activity.py`) both check `visit_logging_allowed` before
creating visits, so the GPX dwell path was the lone importer ignoring `track_pin_visits` - and
that setting's own help text already promises the user it covers imports. So the answer is "both
toggles gate": `track_routes` governs saving the Route (the user's own track), and
`track_pin_visits` governs the PinVisit rows a dwell writes.

`detect_dwells_and_create_visits` now returns 0 early when visit logging is off. The gate is
inside that function rather than at its caller so any future caller inherits it. Route import
itself is unchanged - a profile that tracks routes but not visits gets the track and no visits,
which a new test asserts explicitly (route row still exists, zero PinVisits). 15/15 pass.
`route_import_allowed`'s docstring no longer claims to cover the bundled visits. Original entry
below.

Three `Profile` toggles all plausibly describe the `PinVisit` rows that
`gpx_tracks.detect_dwells_and_create_visits` creates from an imported track, and only one
gates them:

- `track_routes` — "Save imported GPS routes/tracks." Gates it today, via
  `route_import_allowed`. Its docstring says so deliberately: "GPS route/track import
  (and its bundled dwell-detected visits)".
- `track_pin_visits` — "Log visits to your pins from journal entries, **imports**, and photo
  tagging." Names imports explicitly; does not gate this path.
- `track_geolocation` — "Record visits from your live device location." Did not gate it either,
  yet the rows were stamped `source=GEOLOCATION`.

The provenance half is fixed: those rows are now `VisitSource.HISTORY` ("Imported"), matching the
enum's own documentation and what the Google Takeout importer already writes. That removes the
worst of the inconsistency — a row claiming a provenance whose setting had no say over it.

What remains is a product decision, not a bug fix: **should a user with `track_pin_visits` off
still get visits from a route import?** The settings page lists all four toggles together, so a
user who reads "imports" under `track_pin_visits` and turns it off will reasonably expect no
visits from importing a GPX file. Against that, `route_import_allowed`'s docstring states the
current bundling is intentional. Deliberately not changed here, because tightening it would
silently stop creating rows for users who have `track_routes` on and `track_pin_visits` off, and
that trade belongs to whoever owns the settings copy.

If the answer is "both must be on", the change is one `and` in `save_routes_streaming`; if it is
"track_routes alone owns it", `track_pin_visits`' help text should stop advertising imports.

## PARTLY RESOLVED 2026-08-15: `get_or_create` without a backing unique constraint

**Links are done** (migration 0047); `Label` was already done earlier (0042/0043).
**Still open: `SafetyContactOptOut` and `PinVisit`** - see the original entry below for both.

`PinLink` and `WikiLink` now carry `UniqueConstraint(F(owner), MD5("url"))`. The URL is **hashed
rather than indexed directly** - it holds up to 2000 characters, and a btree entry over that in
multibyte UTF-8 can exceed Postgres' ~2704-byte row limit, so a plain unique index would have
traded a duplicate row for an *insert failure on long URLs*, which is worse.

The migration keeps the lowest-pk row per group. Links have no dependent rows, and the survivor is
the one every existing reader already returned via `.filter(...).first()`.

**Adding a constraint is the easy half; the call sites were the real work.** Six write paths
existed, and only one was already safe:
- `external_links.py` (both helpers) - kept the `exists()` check as a fast path that avoids a
  savepoint, and now absorbs `IntegrityError`. These run inside a `LocationCache` signal on a
  Celery queue, where an escaping error is a task failure.
- `pin_subresources.create_pin_link` - raises a new `LinkExistsError`, mirroring the existing
  `AliasExistsError` precedent right above it.
- `controllers/links.py` (wiki add) - returns 400, and deliberately writes **no `WikiEdit`** on the
  duplicate path; recording an edit that changed nothing would leave a phantom revision.
- `external_api/views_wiki.py` - returns 400; `_SUBRESOURCE_ERROR_STATUS` maps `LinkExistsError`
  to 409, matching `AliasExistsError`.
- `pin_suggestions.py` - its in-call `existing_urls` set does not cover a concurrent accept.
- `google/maps.py` - already caught `DatabaseError`; unchanged.

Without those, a user adding a link they already had would have gone from a harmless duplicate to
a 500. `test_external_link_duplicates.py` was inverted from "tolerates pre-existing duplicates" to
"the database refuses them", plus a race test that neuters the fast path to prove the loser gets
False rather than an exception. 30 tests on a fresh DB, 121 in the surrounding link suites.

### Original entry (SafetyContactOptOut and PinVisit still apply)

Five models are looked up with `get_or_create` on a field combination the database does not
enforce as unique. Two concurrent callers both miss, both insert, and the duplicate is permanent —
after which `get_or_create` raises `MultipleObjectsReturned` on every later call for that key.

| model | lookup | call site |
| --- | --- | --- |
| `PinLink` | `(pin, url)` | `services/locations/external_links.py` |
| `WikiLink` | `(wiki, url)` | `services/locations/external_links.py` |
| `Label` | `(kind, name)` / `(kind, name, profile)` / `(name, profile)` | three call sites, three different keys |
| `SafetyContactOptOut` | `(owner, checkin, contact_profile, email, scope)` | `services/visits/safety.py` |
| `PinVisit` | `(pin, source, visited_at)` | `services/import_formats/gpx_tracks.py` |

`PinAlias`/`WikiAlias` are **not** in this list — they carry expression-based unique constraints
(`UniqueConstraint(Lower("name"), F("pin"))`) that give the case-insensitive guarantee their
docstrings promise. A scan reading only `UniqueConstraint.fields` misses those, since expression
constraints leave `fields` empty; that is what made this look like a much larger problem at first.

The links pair no longer *raises* — they now check-then-create, so a duplicate stays a harmless
extra row instead of a permanent exception inside a `LocationCache` signal running on the
panel-fetch queue. The race is still open.

Closing it properly means adding unique constraints, and that is the part needing an owner
decision, because it is entangled with user-facing behaviour:

- **The links and labels have plain `create()` call sites** driven by "add a link"/"add a label"
  UI (`controllers/links.py`, `controllers/aliases.py`, `external_api/views_wiki.py`,
  `services/pins/pin_subresources.py`, `pin_suggestions.py`). A unique constraint turns a user
  adding a URL they already have into an `IntegrityError`. Each of those sites needs to catch it
  and render a friendly message first — which is exactly what `add_pin_alias` already does for
  aliases, and is the pattern to copy.
- **`Label` has no single key.** Three call sites look it up three different ways, so what
  uniqueness even means here is a domain question, not a mechanical one.
- **`SafetyContactOptOut` spans nullable columns.** Postgres treats NULLs as distinct, so a plain
  `UniqueConstraint` would silently fail to prevent the duplicates it was added for; it needs
  `nulls_distinct=False` (Postgres 15+).

Per this repo's migration guidance, each constraint also needs a de-duplication step ahead of it
in the same migration, and index creation goes last.

## ~~OPEN 2026-08-12: no Content-Security-Policy is set anywhere~~ RESOLVED 2026-08-15 (`92182388`, report-only first)

**RESOLVED as a phased rollout.** django-csp is added with `CSPMiddleware` after
`SecurityMiddleware`, and the policy is honest about the app as it stands: `script-src` and
`style-src` still carry `'unsafe-inline'` because of the ~99 inline `<script>` blocks and HTMX
`hx-on:` attributes, so the XSS-blocking benefit is deferred - but `object-src 'none'`,
`base-uri`, `frame-ancestors`, `form-action` and a real `img-src` allowlist (tile/imagery hosts
grepped from the templates and TS, not guessed) apply immediately. It ships as
**Content-Security-Policy-Report-Only**; the new `UL_CSP_ENFORCE` Pydantic toggle flips a given
environment to enforcing once its reports are clean. Threading nonces through the templates to
drop `'unsafe-inline'` is separate follow-up work, tied to the inline-JS extraction effort.
Verified: full-page renders still pass with the middleware active. Original entry below.

Found while fixing the SVG upload hole (fixed; see the audit report). The SVG was exploitable
partly *because* there is no CSP: the app sends no `Content-Security-Policy` header from Django or
from nginx, so any same-origin document that executes script does so unrestricted.

The upload hole is closed at the source, so this is now defence-in-depth rather than an active
hole. It is still worth having: a CSP is the control that makes the *next* injection - a template
mistake, a markdown renderer gap, a third-party script - non-exploitable rather than merely
unlikely.

Not added here because a CSP is not a one-line setting for an app like this one, and getting it
wrong breaks the site quietly: this frontend uses inline `<script>` blocks in templates (99 of
them), HTMX's `hx-on:` attributes, Leaflet, and `json_script` payloads, so a first policy needs
either nonces threaded through those templates or a deliberately permissive `script-src` that is
honest about what it does and does not buy. `django-csp` plus report-only mode for a release, to
collect violations before enforcing, is the usual way in.

## ~~OPEN 2026-08-12: refunds and chargebacks never reverse pay-what-you-want access~~ RESOLVED 2026-08-15 (`b453dc42`)

**RESOLVED** with the policy "clawback the money, forgive the access already consumed":
`banking.apply_refund()` decrements `total_paid_cents` clamped at 0 and deliberately never touches
`amount_used_cents`/`usage_covered_until`, and `charge.refunded` + `charge.dispute.closed`
(acting only on `status == "lost"`) are registered in `_HANDLERS`.

**The idempotency subtlety worth remembering**: `charge.refunded` is *cumulative*, so the
controller's existing per-**event-id** dedup is NOT sufficient - a second partial refund arrives as
a new event whose `refunds.data` re-contains the first refund object, which event-level dedup
would happily re-apply. Claiming is therefore per **refund id**, via a new `StripeProcessedRefund`
model (migration 0044) claimed with `get_or_create` inside the view's existing `atomic()`, so the
claim commits with the decrement it caused. Both layers hold: redelivery is stopped by
`processed_at`, a different event carrying an applied refund is stopped by the refund-id row.

55 tests pass (including a hypothesis property that payment-then-full-refund restores the prior
banked balance while consumed usage stays consumed); ruff/mypy clean. **Two known limits, not
defects**: (1) a charge with >10 refunds truncates Stripe's `refunds.data` and is logged rather
than paginated, so the ledger would under-debit in that case; (2) `StripeProcessedRefund` is not
registered in `admin.py` (skipped to avoid a hot shared file), unlike `StripeWebhookEventAdmin` -
worth adding for audit parity. **Operator action required**: the two new event types must be
enabled on the Stripe dashboard endpoint's subscribed-events list; there is no in-code allowlist.
Original entry below.

`services/billing/webhooks.py::_HANDLERS` registers five Stripe event types
(`checkout.session.completed`, `customer.subscription.{updated,deleted}`,
`invoice.payment_{succeeded,failed}`). There is no handler for `charge.refunded`,
`charge.dispute.created`, or `charge.dispute.closed`, and no code anywhere decrements
`total_paid_cents` - the field is documented as "cumulative amount actually paid via Stripe
invoices, **ever**" and is only ever incremented.

For pay-what-you-want roles that field *is* the entitlement: `services/billing/banking.py` grants
a period while `total_paid_cents >= amount_used_cents + that period's threshold`. So a payment
that is refunded or successfully disputed leaves the access it bought fully intact, and it keeps
counting down on the normal schedule until the banked balance runs out. Cancelling the
subscription does not help - that is deliberate ("you paid for it, you keep it until it runs out",
per `advance_pwyw_usage_ledgers`), and it is exactly what makes the refund case leak.

Nothing is broken today; this is an unhandled case, not a defect in what is handled. The rest of
the billing path is notably careful - signature verified against the raw body, fails closed (503)
when the secret is unset, per-event idempotency under a row lock, raw payload recorded in its own
transaction so a failing handler still leaves something to debug from.

**Not fixed here because the remedy is a policy choice**, not a refactor: whether a refund claws
back the full credit, a pro-rata share, or nothing until a dispute is *lost*; and whether access
already consumed is forfeited or forgiven. Whichever is chosen, the mechanical part is small - a
handler that decrements `total_paid_cents` by the refunded amount, reusing the existing
idempotency, since Stripe delivers these as ordinary events with their own ids.

## ~~OPEN 2026-08-12: the games feature gate exists on the hub only, not on the games~~ RESOLVED 2026-08-15 (`7a652cfa`)

**RESOLVED by gating the games**, which is what `SiteFeature.ALPHA_FEATURES`'s own definition
comment describes ("Gates access to features still under active development") - so the product
question this entry raised is answered by the enum, not left open. A new
`AlphaFeatureRequiredMixin` raises `PermissionDenied` for users without the feature and is applied
to every game view class across `spotguessr.py`, `trivia.py` and `consensus.py`, with
`GamesOverviewView`'s inline check replaced by the same mixin. It sits **after**
`LoginRequiredMixin` in the MRO so anonymous users still get a login redirect rather than a 403.
Gameplay test fixtures grant the feature through a shared helper, constructing a throwaway first
user so the probe account is not the auto-promoted site admin. Original entry below.

`SiteFeature.ALPHA_FEATURES` gates two things: the nav item (`context_processors.show_games_nav`)
and the hub view (`controllers/games.py::GamesOverviewView`, which raises `PermissionDenied`).
Every one of the ~49 views behind it - all of SpotGuessr, Trivia and Consensus, including their
lobby, session, answer and end-game routes - checks only `LoginRequiredMixin`.

Measured on a user who is genuinely not a site admin (this matters - see below):

```
is_site_admin: False
site default_features: []
user_has_feature(ALPHA_FEATURES): False
  /dashboard/spotguessr/      -> 200
  /dashboard/games/trivia/    -> 200
  /dashboard/games/consensus/ -> 200
```

So a user without the entitlement sees no games nav, is refused at `/games/`, and can then open
any game directly and play it.

**Whether that is a bug is a product question, and the existing tests suggest it may be intended.**
A mixin applying the hub's check to all 49 views was written and then **reverted**, because it
broke 9 existing tests that exercise full gameplay - guesses, answers, session end, non-participant
404s - with users who do *not* hold the feature. No existing test asserts that a game refuses a
non-entitled user; `test_games_controller.py::test_requires_alpha_features` covers the hub alone.
That is the behaviour the suite encodes, so tightening it is a deliberate product change rather
than a defect fix, and it would lock out anyone currently playing.

*Re-verified 2026-08-14 (chunk 336):* an AST pass over the game controllers finds **50 view
classes - 1 references the feature gate (the hub), 49 check only `LoginRequiredMixin`**, matching
this entry's count exactly. The gap has not narrowed since it was filed.

If the gate is meant to cover the games, the mechanical part is small: a `dispatch()` mixin mixed
in *after* `LoginRequiredMixin` (so anonymous visitors still get the login redirect rather than a
bare 403), applied to the 49 `(LoginRequiredMixin, View)` classes, plus granting the feature in
those 9 tests' fixtures. If the gate is meant to cover only discovery, then `GamesOverviewView`
raising `PermissionDenied` is arguably too strong for what is really a nav-visibility rule.

**A trap for anyone measuring this:** `user_has_feature` short-circuits to True for
`dashboard.view_site_admin`, and this project promotes the **first** user to site admin. A probe
that calls `baker.make("auth.User")` once measures an admin and concludes the feature is granted
by default - which is exactly what the first attempt here reported before the second user was
added.

## ~~OPEN 2026-08-12: login lockout is identifier-only, so it doubles as a targeted DoS~~ RESOLVED 2026-08-15 (`ea366476`)

**RESOLVED**: a per-IP failure throttle now runs alongside the identifier lockout, reusing the
cache-counter pattern already in `account.py` for the passphrase throttle and the existing
`_client_ip` helper. It is checked before authentication and incremented on every failure, and
deliberately **not** cleared on success (a failure-only window that simply expires). The entry's
"needs a human to pick a number" concern is resolved by making it an admin-tunable
`SiteSettings.login_ip_max_attempts` (default 30, 0 disables) rather than hardcoding a NAT-hostile
constant. The refusal reuses the identifier-lockout error text verbatim, so the no-enumeration
property holds - a test asserts the two responses' error strings are equal rather than pinning a
literal. Original entry below.

`controllers/account.py` locks an account after `login_max_attempts` consecutive failures
(default 5) for `login_lockout_minutes` (default 15). The lockout key is derived **only** from the
submitted identifier - `_lockout_key_for_identifier` resolves it to a user when one exists and
otherwise hashes the raw string. There is no IP dimension, no `limit_req` in the nginx config, and
no throttle on the login view itself.

So anyone who knows a username or email can hold that account out of password login indefinitely
at a cost of ~5 requests every 15 minutes. Passkey (WebAuthn) and social login are separate views
and are unaffected, so the impact falls on password-only accounts.

Two things the current design gets right, worth not regressing:

- **No user enumeration.** A non-existent identifier is rate-limited exactly like a real one, and
  the error text is identical, so the lockout cannot be used to test whether an account exists.
- **Failures only.** A successful login clears the counter (`_clear_login_attempts`).

**Not fixed here because the threshold is an ops decision.** The standard remedy is to keep the
identifier lockout *and* add a per-IP failure throttle - and this codebase already has the pieces:
`_client_ip()` plus the cache-counter pattern used by `suggest_passphrases`
(`_PASSPHRASE_RATE_LIMIT`) and the password-policy check, two functions away in the same module.
Applying it to the *lower*-value endpoints but not to authentication is the asymmetry worth
resolving. What needs a human is the number: too tight and a corporate NAT or a shared campus
address locks out real users, which is the same availability problem from the other direction.

## ~~OPEN 2026-08-12: importing the same calendar event twice creates two trips~~ RESOLVED 2026-08-15

**RESOLVED**: `TripCalendarLink` now carries a partial
`UniqueConstraint(("profile", "google_event_id"), condition=~Q(google_event_id=""))`
(migration 0046). Partial is load-bearing: a *timed* import deliberately leaves the trip-level
link's event id blank so the activity-level row owns the id (see the long comment in
`_create_trip_from_event`), and a plain unique constraint would have broken the second such
import outright - a test now pins that those blank-id rows still coexist.

The service side no longer relies on the `already_linked()` read winning: the trip, membership,
activity and both links are created inside one `transaction.atomic()`, and an `IntegrityError`
rolls the whole half-built trip back and reports the same "already linked to a trip" skip the
fast path produces. That required extracting `_create_trip_from_event()` so the unit could be
rolled back as a whole.

**Live-data decision** (the entry withheld this): the migration deletes only duplicate *link*
rows, keeping the oldest per (profile, event), and deliberately leaves the duplicate Trips
themselves intact but unlinked - a trip may already carry the user's own activities, members or
comments, so destroying it to satisfy a constraint would lose real work. Keeping the oldest
favours the trip the user has had longest. 4 targeted tests pass (72 in the file).
**Pre-existing, untouched**: `test_calendar_sync.py:90` trips ruff PT027 (unittest-style
`assertRaises`) - present in HEAD, unrelated to this change. Original entry below.

`services/trips/calendar_sync.py` guards the import path with
`TripCalendarLink.objects.already_linked(profile, event_id)`, whose docstring states the intent
plainly: "True if a link already exists (import/export already ran for this event)". That check is
`filter(profile=profile, google_event_id=event_id).exists()` - a read at line 583, followed by a
`Trip` create and a `TripCalendarLink` create at 623/633, with nothing serialising the pair.

The model's unique constraints are `(trip, profile)` and `(trip, profile, activity)`. Neither
covers `(profile, google_event_id)`, so two imports of the same event produce **two different
trips**, each with its own link row, and no constraint is violated. Confirmed against a real
database rather than inferred from the model: creating two links with the same
`(profile, google_event_id)` and different trips succeeds, leaving one calendar event mapped to two
distinct trips. The user-visible result is a duplicated trip - exactly what the check exists to
prevent.

Reachable by ordinary means rather than a contrived race: a double-submit, a retry after a slow
response, or the same event selected in two tabs. It is not reachable from the periodic task -
`push_auto_synced_trip_changes` pushes trip changes *out* to Google and does not import.

**The obvious fix is wrong.** A plain `UniqueConstraint(fields=["profile", "google_event_id"])`
would break normal use, because a *timed* import deliberately stores an empty `google_event_id` on
the trip-level link (line 627) and puts the real id on the activity-level link. That is a
documented decision, not an oversight - the comment above it explains that a trip-level link
carrying the event's id would make the next export convert the user's timed appointment into an
all-day event. Empty strings are not distinct to a Postgres unique index, so the constraint would
reject every profile's *second* timed import. Verified: two such rows coexist legitimately today.

A correct constraint therefore has to be partial - unique on `(profile, google_event_id)`
`condition=~Q(google_event_id="")` - which is a new index rather than an upgrade of the existing
plain `idxdb_tcl_profile_event`.

**Not done here** because the migration must also delete rows to apply: any pre-existing duplicate
has to be resolved first, and choosing which link survives decides which of two real trips stays
attached to the user's calendar. That is a call about live user data, not a refactor.

## ~~OPEN 2026-08-12: `date.today()` bypasses Django's configured timezone~~ RESOLVED 2026-08-15

**RESOLVED**: all nine non-test call sites now use `django.utils.timezone.localdate()` -
`controllers/trip.py`, `controllers/tools.py` (×2), `controllers/pin.py`,
`controllers/pin_edit.py`, `services/trips/trip_activities.py`,
`services/import_export/export.py` (×2), `services/ai/link_extraction.py`; four of those files
needed a `from django.utils import timezone` added. Each was converted individually rather than
by a mechanical sweep, since several still legitimately need the `date` import for `date(...)`
construction.

This entry argued for deferring until per-user timezones exist. That reasoning does not hold for
the *server*-side bug: `date.today()` reads the host OS clock, which is not `TIME_ZONE` even
today, so the deployment's own configured zone was already being ignored. Per-user timezones
remain future work and are unaffected by this change.

One regression test guards the most user-visible site (the trip-activity completion clamp):
`TIME_ZONE="Pacific/Kiritimati"` (UTC+14) with `timezone.now` patched to `2026-01-01T20:00Z`,
where the configured zone is already Jan 2 while UTC is still Jan 1 - so a `date.today()` clamp
caps a legitimately-"today" completion a day early. 183 tests pass across the touched modules.
Deliberately no trivial per-site assertions for the other eight. Original entry below.

Nine non-test call sites use `datetime.date.today()`; ten others use `timezone.localdate()`.
`date.today()` reads the *operating system* clock, whereas `localdate()` reads Django's `TIME_ZONE`.
They agree today only because three independent things happen to line up: `TIME_ZONE = "UTC"`, the
container's OS clock is UTC, and `Profile` has no per-user timezone field. Change any one and the
two sets of call sites disagree, silently and only near midnight.

Where it would show first (user-visible, not cosmetic):

- `services/trips/trip_activities.py:818` clamps a completion date to "today"
- `controllers/trip.py:1515` decides which activities count as upcoming for the weather forecast
- `controllers/pin.py:185` / `controllers/pin_edit.py` bound a date input

**Same latent dependency, second form (added 2026-08-13).** Two sibling paths that both turn a form's
date+time fields into an aware datetime use *different* patterns for it:

- `controllers/safety.py:565` guards - `if checkin_by.tzinfo is None: checkin_by = checkin_by.replace(tzinfo=UTC)`
- `controllers/visits.py:229` does not - `datetime.fromisoformat(iso_str).replace(tzinfo=UTC)`, unconditionally

The unguarded one is safe *only* because its string is assembled from separate `<input type="date">`
and `<input type="time">` values, which never carry an offset, so `fromisoformat` always returns a
naive value. Were that ever fed an offset-bearing ISO string, `.replace(tzinfo=UTC)` would **discard**
the offset and reinterpret the wall-clock as UTC - shifting the visit by the user's offset - where
`.astimezone(UTC)` would convert it correctly.

Both are also equivalent to Django's `timezone.make_aware()` only while `TIME_ZONE = "UTC"` and no
per-user timezone is activated (verified: no `timezone.activate()` call exists anywhere outside
tests). Adding per-user timezones - plausible for a travel/mapping app - makes `make_aware()` follow
the user and `.replace(tzinfo=UTC)` silently not. That is the same single dependency this entry is
already about, which is why it is recorded here rather than as its own item.

Every `datetime.fromtimestamp()` call in the codebase passes `tz=UTC` explicitly, and there are no
`datetime.now()` or `utcnow()` calls outside tests, so the rest of that surface is clean.

**CONVERTED 2026-08-14 (audit chunks 316-317), overriding the decision below - read this first.**
*Verified 2026-08-14 (chunk 333): 942 tests pass across every suite touching the nine changed files, plus 3 boundary tests.*

*Half-converted comparison found 2026-08-14 (chunk 334).* `controllers/trip.py:1515` now reads
`today = timezone.localdate()` and compares it against `act.scheduled_at.date()`, which is the
**UTC** date of an aware datetime. Before the conversion both sides were UTC and consistent; now
one side follows the active timezone and the other does not. They agree only while no timezone is
activated - which is true today, so this is latent, not live.

This is exactly the non-uniformity the "deliberately not converted" argument below predicted:
converting a `date.today()` in isolation can leave a *comparison* half-migrated even when the call
itself is correct. Whoever does the per-user-timezone work must treat both sides of this
expression, not just the `localdate()` call. All nine have now been checked (chunk 335), and `trip.py:1515` is the only
half-migrated one:

- `tools.py` (x2) and `export.py` (x2) - the date only formats a **download filename**; no
  comparison exists to be half-migrated.
- `link_extraction.py` - compares a parsed **year** against `localdate().year + 1`. A one-day
  offset can only matter across a New Year boundary, and the `+1` tolerance absorbs it.
- `pin_edit.py` / `pin.py` - mix the same way `trip.py` does (`localdate()`-derived bounds vs a
  UTC-derived `last_visited.date()`), but the bound is a **100-year** range on a date input, so a
  one-day difference cannot change the outcome. Structurally mixed, practically inert.
- `trip_activities.py` - **correctly improved, not mixed.** `completed_date` is a user-supplied
  calendar date, so pairing it with the user's `localdate()` is the right frame on both sides.
  This is the site this entry named as consequential, where the server's "today" could clamp a
  late-evening completion back to yesterday.
All nine sites now use `timezone.localdate()`. This was done *without noticing this entry*, which had
already identified the same nine sites and argued against exactly this sweep. The argument was sound
and its prediction was accurate: the sweep did introduce an undefined-`timezone` `NameError` in
`services/ai/link_extraction.py`, caught by ruff rather than by review. The `export.py` shadowing
hazard named below was already gone, so no wrong-`timezone` reference occurred.

What stands unchanged is the deeper point in the final paragraph: under `TIME_ZONE = "UTC"` with a
UTC container clock and no per-user timezone, this conversion is **behaviour-neutral** - it prevents
no bug that can currently occur. And when per-user timezones arrive, `localdate()` will be no more
correct than `date.today()` was; "today" will have to resolve in the *viewer's* zone, and all nine
sites will need revisiting regardless. The conversion is therefore a small correctness-of-intent
improvement, not the fix, and it should not be read as closing this item.

Reverting is a reasonable call if the project prefers to hold the line until that work happens; the
changes are isolated to the nine call sites plus three added imports.

**Deliberately not converted.** The rewrite changes no behaviour under the current settings, and
the sites are not uniform: `services/import_export/export.py` imported `timezone` *from datetime*
(shadowing Django's, now removed), `controllers/pin.py` imports `date` inside the function body,
and others import the `datetime` module rather than the name. A mechanical sweep across those is
more likely to introduce a `NameError` or a wrong-`timezone` reference than to prevent a bug that
cannot currently occur. The right moment to do it is when per-user timezones are added, since that
work has to revisit every one of these sites anyway - at which point neither `date.today()` nor
`localdate()` is correct, and "today" has to be resolved in the *viewer's* zone.

## ~~OPEN 2026-08-12: trip location visibility re-implements the shared gate, and is stricter~~ RESOLVED 2026-08-15 (`f8d2de98`)

**RESOLVED**: the entry's core risk ("no test pins the stricter behaviour as intentional") was
closed by `03383698` (`tests/hypothesis/test_trip_visibility_is_stricter.py` pins both
divergences, each with a precondition assert that `Profile.visibility_permits` answers the
opposite way), and the remaining gap - no in-module statement that the divergence is deliberate -
is now a paragraph in `trip_visibility.py`'s module docstring naming both differences, that they
fail closed, and that loosening either is a product decision to be made in the same commit that
updates those tests. The divergence itself is intentionally unchanged. Original entry below.

`Profile.visibility_permits` is documented as the "shared evaluator for every per-field
`VisibilityChoice` setting on this model ... so the friend/common-pin/common-friend/common-trip
relationship queries live in exactly one place". `services/trips/trip_visibility.py` does not use
it. It buckets activities by the adder's `trip_pin_location_visibility` and resolves each bucket
with its own queries - deliberately, to answer for a whole list of activities in a fixed number of
queries instead of one evaluator call per activity.

The re-implementation is **stricter than the canonical evaluator in two ways**, both confirmed
against a real database rather than inferred:

1. **Pending friend requests.** `visibility_permits` grants access when the subject has an
   unanswered request *to* the viewer ("asking someone to connect deliberately lets them see who is
   asking"). `trip_visibility` only ever loads `ACCEPTED` friendships, so the same pair resolves
   `permits=True` / `hidden=True`.
2. **`COMMON_PIN` means something narrower.** `visibility_permits` asks whether the two profiles
   share *any* pinned location; `trip_visibility` asks whether the viewer has a pin at *this
   activity's* location. A viewer who shares a pin elsewhere is permitted by the evaluator and
   hidden by the trip rule. (The module docstring says "shares the pin", so this one reads as
   intended - it just is not the same predicate.)

Both differences **fail closed**, so neither is a leak, and that is why this is filed rather than
changed: making the two agree would *reveal* locations currently hidden, which is a product call
about other people's privacy, not a refactor.

The risk is the opposite direction. A future cleanup that notices the duplication and "unifies"
these onto `visibility_permits` - exactly what that method's own docstring invites - would silently
widen who can see trip-mates' locations, with no test failing, because no test currently pins the
stricter behaviour as intentional. If the divergence is intended, it belongs in the module
docstring next to the existing `COMMON_TRIP`/`ANYTHING_IN_COMMON` note, which does explain its
reasoning.

## ~~OPEN 2026-08-13: undoing a pin delete does not bring back its comments, albums or links~~ RESOLVED 2026-08-15 (`966d924e`, honest-wording option)

**RESOLVED via this entry's own "cheaper alternative"**: the app no longer over-promises. The
delete-pin confirm dialog now says "The pin and its photos can be restored from Settings → Undo
History. Comments, albums and links are deleted permanently." (photos claim verified:
`handlers/pin.py` serializes `image_ids` and reattaches on restore since `Image.pin` is SET_NULL),
and the two docstrings claiming full-subtree restorability (`models/pin/viewset.py`,
`services/pins/pin_edit.py`) were corrected. Deep-graph restore (PinNote/Link/Alias/PinVisit
first, comments/albums as documented exclusions) remains a possible future feature - the sketch
lives in the original entry below - but is deliberately not promised anywhere in the UI now.

`Image.pin`, `MarkupMap.pin` and `TripActivity.pin` are `SET_NULL`, so deleting a pin deliberately
preserves the user's irreplaceable content and merely detaches it. Everything else FK'd to Pin
CASCADEs: comments, albums, map overlays, custom layers, links, notes, visits, aliases, reviews.

`PinUndoHandler` serialises the pin's own fields, its FK ids and its label ids - and, as of
2026-08-13, the ids of the photos that survive detached, so an undo re-links them. It does not
serialise anything that CASCADEs, so those rows are gone for good the moment the delete commits.
Measured: a pin with one comment and one album, deleted and immediately undone, comes back with
`comments=0 albums=0`.

Whether that is wrong is a product call, which is why this is filed rather than changed:

- The undo framework's own docstring points readers at each handler for "exactly what is and isn't
  restorable", and `PinUndoHandler` describes its scope as the pin and its detail-pin subtree. Read
  strictly, dependent content was never in scope.
- But the delete dialog tells the user a subtree is "all of it restorable from Undo History", and a
  user who deletes a pin by mistake and immediately undoes will not expect its comment thread to
  have evaporated.

Doing it properly means serialising whole object graphs (a comment carries reactions, a markup map,
mentions; an album carries ordered items) and restoring them with fresh pks while preserving
internal references - a much larger change than the photo re-link, and one that needs a decision
about how deep "undo" reaches before it is worth building. The cheaper alternative is to stop
promising it: narrow the delete-confirmation wording to say the pins come back and the discussion
does not.

**Partly resolved (chunk 461, 2026-08-15): the promise is narrowed.** The delete dialog now says
the pin and its photos come back and its comments, albums and links do not; the two docstrings
claiming "all of it restorable" now state the real scope and point at `PinUndoHandler`. The
deep-restore question (serialising whole CASCADEd object graphs) remains the open product call
above.

## PARTLY RESOLVED 2026-08-13: the generated OpenAPI schema has 224 enum-naming collisions

`manage.py check --deploy` reports 237 non-security issues, all from drf-spectacular: **224 W001**
(enum naming) and **13 W002** (views it cannot infer a serializer for).

The W001s matter more than "warning" suggests, because this schema is what a native client generates
its types from. Two shapes:

- *Multiple names for one choice set* - e.g. `FriendshipStatusEnum`, `MapDarkModeEnum`,
  `SecurityEnum` are each derived more than once. Technically correct, but a generator may emit
  duplicate types.
- *Unresolvable collisions*, which drf-spectacular papers over with a hash: fields named `status`
  became `Status0ebEnum`, `Status770Enum`, `Status9a4Enum`, `StatusA4dEnum`, `StatusEa9Enum`, and
  `kind` became `KindE9eEnum`. A client consuming that schema gets five unrelated,
  meaninglessly-named status enums, and the names are not stable - they are derived from the
  colliding set, so adding a sixth `status` field can renumber the others and silently change a
  generated client's type names.

The fix is mechanical but not small: add `ENUM_NAME_OVERRIDES` entries to `SPECTACULAR_SETTINGS`
mapping each choice set to a stable, meaningful name. It is worth doing before a client is generated
from this schema rather than after, since renaming afterwards is a breaking change for that client.

The 13 W002s are `APIView` subclasses drf-spectacular cannot introspect (the E2EE key views, a few
reaction/revert endpoints); each is simply omitted from the schema, so those endpoints are
undocumented rather than wrongly documented. Adding `serializer_class` or an `@extend_schema`
annotation fixes them individually.

Not urgent, and not a runtime defect - filed because it is invisible from inside the app and only
shows up when someone generates a client.

## PARTLY RESOLVED 2026-08-13: the hardened fetch helper is used by 11 call sites out of ~136

`frontend/ts/shared/fetch-json.ts` exists precisely to fix a class of bug its own docstring names -
"the ``!resp.ok`` check the mutating calls in the very same file were missing". It checks
`response.ok`, extracts a server error message for a toast, distinguishes offline from HTTP failure,
and supports a timeout. It has 22 tests. `entries-classic/core.ts` installs it globally as
`window.ulFetchJson` / `window.ulSendJson`.

**11 template call sites use it. 125 raw `fetch(` calls in templates do not.**

Of those 125, 17 have neither a `response.ok` check nor a `.catch` within 14 lines. Three were read
to check the flag is meaningful:

- `pages/safety/home.html:17` - a false positive; the match is inside a Django comment.
- `pages/trips/detail.html:717` - real. `fetch(url).then(r => r.json()).then(...)` with no `.catch`.
  A network failure or 500 rejects unhandled, so the trip map never renders *and* the
  `_showEmptyMap()` fallback inside the success path never runs either. The user gets a blank panel
  and no explanation.
- `partials/pins/pin_share_dialog.html:198` - real, and worse. `fetch(...).then(r => r.text())` with
  no `.ok` check, then `grid.innerHTML = html`. On a 500 the body *is* Django's error page, so the
  error markup is injected into the share dialog.

So roughly two-thirds of the sampled flags are genuine; the honest read is "a real cluster of
unhandled fetches", not a precise count of 17.

**Scope correction (same day):** that sweep covered `frontend/ts/**` and `templates/**` but not
`frontend/static/js/**` - five hand-written JS files that ship as-is. Re-checked: `cover-hero.js`
guards its `JSON.parse`, `article-editor.js` does check `!resp.ok`, and two `fetch(` matches in
`pin-select-map.js` are docstring examples. One more genuine site: `pin-select-map.js:133`,
`fetch(opts.dataUrl).then(r => r.json()).then(...)` with no `.ok` check and no `.catch` - a failed
request leaves the pin-selection map silently empty.

This also breaks a documented project standard - `CLAUDE.md`: "Results and errors must surface as
toast notifications."

Not fixed here: 125 call sites across templates with no frontend tests covering them is a migration,
not an edit, and the two worst examples above are enough to decide whether it is worth scheduling.
The mechanical part is small per site (`fetch(u).then(r => r.json())` becomes
`window.ulFetchJson(u)`), but each one needs its error path chosen - toast, empty state, or silent -
and that is a judgement per feature.

**Partly resolved (chunk 491, 2026-08-15): the three named defects are fixed.** The trip map's
failure path now shows the empty state plus a toast (its existing `.catch` only hid the wrapper,
silently - the blank panel the entry described); the share dialog checks `r.ok` before injecting
(a 500's Django error page can no longer become dialog markup) and toasts on failure; the
pin-selection map toasts instead of staying silently empty. All three verified with `node
--check`; 394 TS tests pass. The 120-odd-site migration itself stays filed - per-site error-path
judgement, as the entry says.


---

## ~~OPEN QUESTION 2026-08-14: does the external API apply trip-activity location masking?~~ (ANSWERED same day - both gates ARE applied)

Found by checking the six-mechanism inventory above against the newest surface (audit chunk 396).
Across 69 `external_api/` files:

| gate | direct uses in external_api |
|---|---|
| `identity_visibility` (profile masking) | 5 files |
| `wiki_access` (place-domain) | 7 files |
| `visible()` (device scans) | 1 file |
| `*_for_viewer` | 1 file |
| **`viewer_hidden_activity_ids` (trip activity locations)** | **0** |
| **`display_identity_for` (DM sender names)** | **0** |

**Zero direct uses is not itself a defect** - the API imports from `services.trips.*` and may inherit
masking through delegation. But it is exactly the recurring shape this codebase's history shows: a
newer surface that does not consult the gate its subsystem already has (see the Google Calendar
export, the data export, and reply/reaction notifications, all of which failed this way).

**Unresolved.** Answering it means following `serializers_trips.py` / `serializers.py` to whether a
trip activity's coordinates reach an API response for a viewer the internal UI would hide them from.
That trace was not completed. Two concrete checks would settle it:

1. Does any external-API trip serializer emit activity coordinates without passing through
   `trip_visibility`?
2. Does the DM/group-chat API emit sender names without `display_identity_for`?

Both have a natural test: a viewer who should see a masked identity or hidden location, asserted
against the API response rather than the rendered page.

**ANSWERED 2026-08-14 (chunk 397) - both are masked; the zero-use table was measuring the wrong
thing.**

1. *Trip activity locations*: `external_api/serializers.py` documents masking by "the activity's own
   `location_hidden` flag or by the adder's..." and branches on `effective_location_hidden` when
   serializing. Applied as an **annotation**, not by calling `viewer_hidden_activity_ids`.
2. *DM sender names*: `serializers_messaging.py`'s docstring states "identity masking (the 2026-07-23
   fix): a sender whose ``profile_visibility``..." and "the sender's displayed identity is resolved
   through this viewer's visibility". The `sender_name`/`sender_slug` fields are `read_only` and
   populated upstream where `display_identity_for` runs.

So the six-mechanism table is useful for *finding* the gates but not for auditing whether a surface
uses one: a gate applied via annotation or resolved upstream is invisible to a search for the
helper's name. **Any future check of this kind has to look for the masking's effect, not its call
site.**

**And those behavioural tests already exist (chunk 398).** `test_external_api_trips.py` has
`test_hidden_location_omits_coordinates_entirely`, `test_masked_member_exposes_no_slug` and
`test_comment_visibility_gate_hides_the_whole_comment`; `test_external_api_messaging.py` has
`test_masked_sender_name_is_not_leaked_in_the_thread` and
`test_masked_partner_display_name_is_not_the_username`. The two checks proposed above were already
written, named almost identically, and are passing in the suite. Nothing to add here.

## ~~OPEN 2026-08-14: JSON rendered with `|safe` in `<script>` blocks~~ (DISMISSED same day - `safe_json_for_script` escapes `<`, `>`, `&`)

Found by audit chunk 422, following chunk 421's residual. **Not confirmed exploitable** - the
verification below was not completed - but the shape is specific enough to be worth checking properly.

Seven template values pass server-serialised JSON through `|safe`: `chart_labels`,
`chart_user_labels`, `chart_user_counts`, `chart_total`, `common_pins_json`, `filter_labels_json`,
`pin.tags_data_json`, `pin_list.smart_boundary.geojson`. Four of them sit in templates that contain
`<script>` blocks (`_cost_admin_body.html`, `common_pins.html`, `pages/map/index.html`, `data.html`).

**Why it matters:** `json.dumps` does not escape `<`, so a user-controlled string containing
`</script>` terminates the block early and everything after it parses as HTML. Several of these carry
user-authored names - label names, tag names, pin names.

**Why it is probably fine but needs checking:** Django's `json_script` filter exists for exactly this
and **is already used in 16 templates here**, so the idiom is known and adopted. These sites may
predate it, or may serialise through something that escapes, or the values may not be inside the
script blocks at all.

**Check (1) is now confirmed (chunk 423).** Parsing `<script>...</script>` regions and testing
containment: **all 14 `|safe` JSON expressions are lexically inside script blocks** -
`site_admin_stats.html` (4), `_cost_admin_body.html` (4), `pages/map/index.html` (3), `data.html`,
`common_pins.html`, `detail.html`. None is in an attribute or body context.

**Check (2) is the only thing left.** Two payloads plainly carry user-authored text:
`filter_labels_json` (label names) and `pin.tags_data_json` (tag names); `common_pins_json` carries
pin names. If any is serialised with a plain `json.dumps`, a label named `</script><img src=x
onerror=...>` closes the block and the rest parses as HTML.

**DISMISSED (chunk 424).** The producing code escapes. `controllers/maps.py` builds both
`filter_labels_json` and `tags_data_json` through **`services/core/json_safety.safe_json_for_script`**,
whose docstring states it returns "a JSON string with `<`, `>`, and `&` escaped", via
`DjangoJSONEncoder`. A label named `</script><img ...>` serialises to `\u003c/script\u003e` and
cannot terminate the block.

So the `|safe` usage is correct: the value is already escaped for script context by a purpose-built
helper, and `json_script` would be a second mechanism for a problem already solved. **No action
needed** - this entry is kept as the record of the check, not as an open item.

Original checks, for reference: (1) is the `{{ ... |safe }}` lexically inside a `<script>`
element, and (2) can any string in the serialised payload contain user input? If both, convert to
`{{ value|json_script:"id" }}` and read it from JS via `JSON.parse(document.getElementById(...).textContent)`,
matching what the other 16 templates do.

## ~~OPEN~~ RESOLVED 2026-08-15 (chunk 460): migration 0007's token encryption has the same noop reverse 0039 had

The migrations 0026-0044 irreversibility audit (chunk 459) fixed 0039's in-place field
encryption to carry a real decrypting reverse - `RunPython.noop` there meant `migrate dashboard
0038` *succeeded* while leaving ciphertext where pre-0039 code expects plaintext. Migration
`0007_pinshare_bundled_with_markup_map_removed_flags` encrypts credential tokens
(`encrypt_existing_tokens`) with the same noop-reverse pattern and has the same silent-corruption
rollback. Lower urgency only because rolling back to <0007 is far less plausible than to 0038,
but the fix is mechanical: copy 0039's `_decrypt_column`/shared-columns-constant shape (the
`gAAAA` Fernet-prefix discriminator handles pre-encryption plaintext rows). Credential fields
fail hard rather than soft, so the raising behavior on an undecryptable value is already right.

**Resolved (chunk 460, same day): 0007 now carries `decrypt_existing_tokens`, the exact 0039 shape** (shared column constant, `gAAAA` discriminator, raising failure). Wiring pinned by `test_migration_0039_reverse.py`.

**Partly resolved (chunk 463, 2026-08-15): 307 warnings -> 20, and the schema now documents
authentication.** The bulk was not enums at all - it was one "could not resolve authenticator"
per external-API view, meaning the published schema documented *no auth whatsoever*; an
`OpenApiAuthenticationExtension` for `ApiKeyAuthentication` (external_api/schema.py) now stamps
the bearer scheme on all 281 operations. All six hash-named enums
(`Status0eb/770/9a4/A4d/Ea9`, `KindE9e`) have stable `ENUM_NAME_OVERRIDES` names; full model
choice sets are referenced by import string so they follow the model. Remaining: 3 cosmetic
"multiple names for one set" warnings (technically-correct schema), ~15 operationId collisions
(list-vs-detail on one path prefix, resolved with numerals - stable but ugly), and the 13 W002
serializer-inference errors, all pre-existing.

**Update (chunk 465): 5 of the 13 W002s fixed** - the reaction mixin's PUT/DELETE, the wiki
revert/restore POSTs and the SpotGuessr round-expire POST now declare `request=None` (their
inputs ride in the URL) plus response shapes. The remaining 8 are the E2EE key-distribution
views (`E2EEEnrollView`, `E2EEOwnKeysView`, rewrap/reset, conversation/group/partner key views),
whose request/response bodies are structured key bundles - annotating them honestly means
writing real serializers for those shapes, not `OpenApiTypes.OBJECT`; a client generating types
for E2EE payloads deserves better than `object`. Filed as its own piece of work.

**Done (chunk 471, 2026-08-15): schema errors are now zero.** `controllers/e2ee_schema.py`
defines documentation serializers mirroring each view's actual reads/writes (enroll bundle,
wrapped-key envelopes, group member tokens, rewrap-all inventories, reset confirmation); all nine
schema-visible methods are decorated. The views still parse JSON by hand on purpose - blobs are
opaque size-bounded strings - so these serializers document, never validate. 94 E2EE tests pass
unchanged.
