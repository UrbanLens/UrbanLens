# The public demo instance

A visitor can try UrbanLens without an account. The "Try the demo" button on the
login and registration pages sends them to a **separate deployment** that mints
them a throwaway account, seeded with sample content, and deletes it a day later.

## Why a separate deployment

Isolation is the deployment boundary, not a filter in application code.

The alternative considered first was a `demo_realm` column on `Profile` and
`Location`, with every visibility check taught to compare realms. It was rejected
for two reasons that are worth recording, because the idea will come back:

- It required widening `Location`'s `unique_together` from `(latitude, longitude)`
  to include the realm. That is a schema change to the largest, most
  heavily-referenced table in the app, paid for by data that is deleted within a
  day.
- It required a realm guard in roughly twenty visibility evaluators, each of
  which **fails open**: a guard placed after an existing `ANYONE` short-circuit
  compiles, passes review, and silently does nothing. The blast radius of one
  missed guard is a real user seeing demo content, or worse.

A separate database makes both problems disappear. There is no rule to enforce,
so there is no rule to get wrong.

## What is real and what is fabricated

**Coordinates are never fabricated.** A pin asserts that a real place exists at a
point, and the app acts on that: the detail page resolves boundaries, asks REData
about the parcel, looks for a wiki, and offers to organise the buildings standing
there. Pointed at an invented coordinate every answer is empty, which reads as a
broken product rather than a demo. Pins therefore come only from imported real
locations - see below.

**Nothing personal is ever imported.** Not users, not comments, not articles, not
edit history - those describe people. What travels with a public location is its
coordinates and the non-authored cached material: photos with no uploader, and
aliases.

Everything else - the personas, their friendships, messages, trips, visits and
lists - is fabricated, so the instance looks inhabited rather than empty.

## What gets fabricated

`services/demo/social.py` builds the rest of the account once the pins exist:
accepted friendships between the login account and each persona (and a few
among the personas), a couple of comments on wikis that a second seeded
profile actually shares access to, a plaintext direct-message exchange, one
group chat (memberships created before the messages that depend on them - see
`seed_group_chat`), a short visit history via `create_manual_visit`, a trip
with activities on pooled locations, a couple of pin lists, generated (not
fetched) photos, and awards against whatever `Achievement` definitions already
exist - never new ones, since those are global and a save fans out to every
profile on the site.

Every writer there is chosen specifically because it does not notify: plain
`Friendship.objects.create(status=ACCEPTED)` rather than
`request()`/`accept()`, plain `DirectMessage.objects.create(...)` rather than
`create_direct_message()`, and so on. `bin/check_notification_choke_point.py`
is the structural guard that keeps this true for any writer added later.

**A subtle bug lives here for anyone extending this further.** `seed_demo_account`
patches `safely_enqueue_task` for the call, and several models this seeder
touches (Pin, Friendship, Comment...) defer their Celery enqueue to
`transaction.on_commit` rather than calling it immediately. An
`on_commit` callback runs whatever the *current* function is at the moment the
transaction actually commits - not whatever it was when the callback was
registered. `seed_demo_account` therefore wraps `transaction.atomic()` *inside*
the `mock.patch(...)` context (`with mock.patch(...), transaction.atomic():`),
never the other way around - patching inside the atomic block would mean the
patch has already exited by the time anything deferred actually runs, and
every queued achievement evaluation or notification would fire for real
against a live worker. `SeedingCommitOrderingTests` in
`test_demo_seed_smoke.py` is a genuine `TransactionTestCase` proving this,
because `TestCase` (savepoint rollback, not commit) and even
`captureOnCommitCallbacks` (which defers to when the *test's* block exits,
after the function has already returned either way) cannot exercise it.

## Where the demo's pins come from

Two sources, both real, both landing in the same manifest:

1. **This site's own public locations.** On the real site:

   ```bash
   python src/urbanlens/manage.py export_public_locations --out public.json
   ```

   "Public" means a `PublicPinCandidate` that reached `PASSED` - the outcome of
   the community vote in `services.pins.public_pins`. **A location having a wiki
   does not qualify**, and the distinction is the whole safety argument: wiki
   visibility is *earned* per viewer (you must already hold a pin on that place
   or its place domain), and `resolve_visible_wiki` 404s indistinguishably so
   that guessing slugs cannot reveal which locations other users have pinned.
   Exporting on a "has a wiki" basis would publish every location any user has
   ever pinned.

   As of 2026-08-20 production has **zero** passed candidates, so this exports an
   empty file. That is correct, not a failure.

2. **REData**, via an endpoint published for this purpose.
   `services/demo/locations.py::redata_demo_locations` is the seam it arrives
   through. It currently returns nothing on purpose - a guessed URL is either a
   dead request on every import or, worse, a plausible wrong one returning
   somebody else's data.

Then, on the demo instance:

```bash
python src/urbanlens/manage.py import_public_locations public.json
```

This creates each `Location` and its `Wiki`, then writes the manifest named by
`UL_DEMO_LOCATIONS_FILE`. The manifest is written **after** the import so it can
only name locations that exist locally.

Seeding pins every manifest entry into every new demo account. That pin is also
what grants wiki access, since visibility is earned by holding one.

An empty pool seeds an account with no pins and logs a warning. The instance
still comes up and still signs people in.

## Settings

Set on the **demo** instance:

| Setting | Purpose |
|---|---|
| `UL_DEMO_MODE=true` | Marks this as the demo. Registers `/demo/start/`, shows the banner, and restricts outbound APIs. |
| `UL_DEMO_LOCATIONS_FILE` | Path to the seeding manifest written by `import_public_locations`. |
| `UL_DEMO_REAL_SITE_URL` | The real site, for the banner's "create a real account" link. |

Set on the **real** site:

| Setting | Purpose |
|---|---|
| `UL_DEMO_URL` | Absolute URL of the demo. Empty hides the button, so it can never advertise a destination nobody provisioned. |

`UL_DEMO_MODE` and `UL_DEMO_URL` should never both be set on one instance.

## Outbound APIs

A demo instance may call **REData and nothing else that needs a key**. Enforced in
`rate_limiter.service_is_enabled`, which every outbound call already passes
through via `_reserve_call`, and checked before the cached-config fast path so a
caller already holding a row cannot skip it.

REData is exempt because it is this project's own service: the demo is the thing
it exists to show off, and calling it costs nothing but our own capacity. Every
other provider bills per call, and a demo visitor is anonymous.

Seeding additionally patches `safely_enqueue_task` and writes each profile with
`external_apis_enabled=False` and `ai_enabled=False` *before* any content exists,
so a later background pass cannot pick the rows up and start spending.

## Lifecycle

Every visit mints a new account (`demo-<seed>-0`, plus four personas
`demo-<seed>-1..4`). Sharing one account would mean the first visitor to delete
everything defines the product for everyone after them, with no owner to revert
it, and two concurrent visitors would see each other's edits mid-session.

Expiry is `date_joined + TTL` - no extra column, which is what keeps the real
site free of a migration it has no use for. On the demo instance, on a schedule:

```bash
python src/urbanlens/manage.py purge_demo_accounts --ttl-hours 24 --execute
```

Dry-run without `--execute`, and it refuses to run at all when `UL_DEMO_MODE` is
off: the same image serves the real site, and a username prefix is a weaker guard
than a separate database.

The `demo-` prefix is **reserved** in `username_is_taken`, including against
confusable spellings (`dem0-`). Without that, a real account could register a
name the purge selects on and be destroyed by it.

## Provisioning

One hostname, one certificate - no wildcard. (`dev_env.py` uses a wildcard for
ephemeral per-agent environments; that requirement does not transfer here.)

1. An A record for `demo.urbanlens.org`.
2. A certificate for that name on NPM, proxying to the demo stack's port.
3. A container stack with its own database, running this image with the settings
   above. It shares nothing with production.
4. Import the location manifest, and schedule `purge_demo_accounts`.
