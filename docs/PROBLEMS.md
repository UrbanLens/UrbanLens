# PROBLEMS

Bugs or quirks identified during other work but out of scope to investigate/fix at the time.

> **Referencing this file from code:** name the entry, not just the file. There are 33 source
> references reading `see docs/PROBLEMS.md`, and this document is over 7,000 lines - a bare pointer
> costs the reader a full-text search and, in practice, they do not do it. Prefer
> `see "the documented docker cp resync breaks the app container" in docs/PROBLEMS.md`. Cite **every**
> relevant entry, not the nearest one - `Friendship.muted` had two (wrong shape, and never read; both
> now in the archive), and a pointer to one implies it is the whole story where a bare pointer at
> least led to both. Headings
> are stable here; line numbers are not. **A date works nearly as well as a heading** - `external_api/
> serializers.py` cites "docs/PROBLEMS.md, 2026-07-28" and that alone locates the entry unambiguously,
> because entry headings carry their date. A **distinctive identifier** in the surrounding prose works
> just as well - `external_api/views.py` says only "Recorded in docs/PROBLEMS.md" but names
> `MapController.resolve_place`, which locates the entry immediately. What fails is a bare reference
> whose comment describes the problem only in general words (this file is append-only and grew by ~800 lines on 2026-08-14
> alone).

---

> Resolved entries live in [`PROBLEMS-ARCHIVE.md`](PROBLEMS-ARCHIVE.md). This file is what is
> still open, still partial, or still worth knowing before touching the area it describes.

## OPEN 2026-08-21: production REData 404s on `/api/v1/public-locations/` (and `/capabilities/`)

Verified live against a fresh dev environment (`a962bf8`, `--redata production`, the default as of
this session): `bin/dev_env.py create` correctly reported credentials (`demo / demo-a962bf8`,
confirming the seed-summary parser fix), but seeding logged `REData request to
/api/v1/public-locations/ failed (404)` and fell back to zero catalog pins - only the Hudson River
State Hospital landmark pin was seeded.

Confirmed with `curl` directly against `https://redata.urbanlens.org/api/v1/public-locations/`
(plain HTML 404, not a DRF JSON 404 - the route itself isn't matched) and
`https://redata.urbanlens.org/api/v1/capabilities/` (same). Both routes exist in the local REData
checkout (`../REData`, `src/redata/api/urls.py`, HEAD `6273443` 2026-08-20) and both are the routes
the 2026-08-21 session's work built against - `parcels/lookup/` on the same host returns 401
(route matched, auth/params rejected), so this isn't a credentials problem. Production REData is
answering from a build older than both routes.

This means "production REData by default" for new dev environments currently seeds no real
catalog pins - not a UrbanLens-repo defect, but worth knowing before trusting a fresh environment's
seeded pin count. Resolves itself once REData's production deployment picks up the commit that adds
these routes; nothing to do here in the meantime beyond this note.

## NOT A DEFECT 2026-08-21: `ruff-format` formats the whole repo on any pre-commit run

Recorded because an agent hit this, wrote it up as a hazard, and reverted the formatting - all of
which was wrong, so the correction is worth keeping.

The `ruff-format` hook is declared `always_run: true` with `pass_filenames: false`, so
`pre-commit run --files <a few files>` still formats everything ruff does not exclude. That is
deliberate, and **running `pre-commit` or `ruff-format` is always fine** (Jess, 2026-08-21):
formatting the repository is the intended behaviour and its output should be kept, not reverted.

The only real consideration is timing, and it is mild: a full-repo format touches files other
people may have open. Commit or stash in-flight work first if that matters, then run it and keep
the result. Do not hand-revert formatting to keep a diff small.

## RESOLVED 2026-08-21: an unpinned bun tag broke every container build

`Dockerfile` installed bun with `COPY --from=oven/bun:1` - a floating major tag. Bun **1.4.0**
dropped `bun build --format iife` (`error: Formats besides 'esm' are not implemented`), which
`bin/build-frontend.ts` needs for the `ts/entries-classic/` bundles - the scripts loaded without
`type=module` (`core.js`, `e2ee.js`, `webauthn.js`, `permissions.js`). The moment that tag moved,
**every newly built container failed at `bun run build`** and crash-looped in `init.py`, with no
local change to blame. Hosts that already had bun 1.3.x kept working, so it looked like an
environment-specific mystery rather than a dependency bump.

Found because a fresh `bin/dev_env.py create` environment would not come up (`ul_<slug>_app`
stuck `Restarting`). This was **not** limited to dev environments - a staging or production
rebuild would have hit exactly the same wall.

Pinned to `oven/bun:1.3.14` (matching the host and `bun.lock`), with a comment saying to bump
deliberately after checking `iife` still builds. The durable fix, if bun keeps iife removed, is
to stop needing it: `entries-classic` exists only because those four bundles load as classic
scripts, and the same trap in reverse is what killed the floorplan editor (see the 2026-08-21
`type="module"` entry in ROADMAP.md, UL-405). Worth revisiting as one piece of work rather than
carrying an exact pin forever.

## OPEN 2026-08-21: Consensus points are awarded for reverting someone else's edit, and never retracted

Found while surveying scoring infrastructure for UL-397, not while working on Consensus — so this
is unverified against intent and may be deliberate, but the two halves disagree with each other in
a way that looks accidental.

`models/wiki_edit/signals.py` awards `MANUAL_EDIT_POINTS = 3` on **every** created `WikiEdit` that
has an editor and no `consensus_round`. A revert is itself a `WikiEdit`
(`services/wiki/wiki_edits.py:269`), so **reverting another user's contribution earns the reverter
points**, and in an edit war both sides are paid on every pass. The same signal also fires for
alias/link/markup/child-wiki rows, so those each earn the full 3 as well.

Meanwhile `award_points` (`services/consensus/points.py:78`) is only ever called with positive
amounts and there is **no retraction path anywhere** — a contribution that is later reverted keeps
its points permanently. `services/achievements/metrics.py:398-407` takes the opposite position for
the same underlying data, deliberately excluding `reverted=True` edits from the `wiki_edits`
achievement metric. So the achievement system says a reverted edit doesn't count and the points
system says it does.

Not fixed here because the fix depends on a product call (should reverting be worth anything? is
an alias worth the same as an article edit?) and because the points ledger has no per-award record
to retract against — `award_points`' `reason` argument is logged, never persisted, so there is
currently no way to know how many points a given edit produced. Both are addressed by the UL-397
design (`docs/designs/reputation-and-gating.md`), but that is a separate, hidden score; whether
the *visible* Consensus game score should also change is its own question.

## OPEN 2026-08-21: a wiki with zero description/dates/security/links has no way to add its first link

`_wiki_about_card.html`'s outer guard - `{% if wiki.description or wiki.date_abandoned or
wiki.effective_date_last_active or wiki.links.exists %}` - renders nothing at all, including the
links row, when every one of those is empty. The only "add a link" entry point lives inside that
row (see the `dialog_id` docs on `_pin_links_row.html`, fixed 2026-08-21 to open the shared
add-link dialog instead of an always-visible inline form), so a wiki that has never had a
description, dates, security indicators, or a link set on it renders no card and no way to add a
first link short of using "Suggest Edits" to set some other field first. Pre-existing - the same
guard gated the old inline form identically - not introduced by that fix, just still open.

## RESOLVED 2026-08-19: `inspects_content` stopped the two tabs it was written to hide

`ed8b3b28` ("a panel tab appears only when it has something to show") gave `photon` and
`open_elevation` an `inspects_content` flag, so a fresh cache row holding a legitimately empty answer
no longer counts as ready. Correct for the tab strip, which is what the commit was about.

But `PinController.location_data_overview` used `is_ready` to mean *"has this source been fetched
yet"*, and those are different questions. After the commit the two panels looked unfetched forever:
rescheduled on every render, and - because `empty_keys` is only appended inside the `is_ready`
branch - never reported to the client. `empty_keys` drives the `pinLocationDataEmpty` HX-Trigger,
which is exactly what `_pin_location_data_tabs.html` uses to hide a dead tab. So the fix that
introduced `inspects_content` prevented the two tabs it targeted from ever being hidden, and made the
Overview poll for them indefinitely.

The endpoint now reads a fresh `LocationCache` row directly and treats its presence as "asked";
`is_ready` keeps its narrower meaning for the tab strip.

**How it stayed hidden for a day:** two tests in `test_location_data_overview.py` (written 2026-07-22)
did catch it - they assert the 204 and the full `empty_keys` set - and had been failing since the
commit landed at 00:29. They were only noticed because a broad run happened to include that file. A
behaviour flag that changes what an existing helper means is worth grepping for callers of that
helper; `is_ready` had two, and only one wanted the new meaning.

## OPEN 2026-08-20: the mobile panel's `unpinned_count` still counts what the import won't create

`ParcelBuildingsPanelSource.api_payload` derives `unpinned_count` as
`sum(1 for row in rows if not row["child_name"])`, and its own comment says that is meant to count
"what the 'add buildings' dialog would actually offer ... see pin_restructure.missing_buildings".
Those two answers have now diverged: `missing_buildings` also excludes a building standing on a point
the owner has already pinned with a *non-child* pin, because `resolve_child_pin_location` refuses to
create a second pin there (the web-side bug fixed 2026-08-20 - the button offered a building that
could never be created, and every attempt silently skipped it).

The web panel's count was repointed at `missing_buildings`; this one was not, deliberately. The
payload ships its `buildings` rows *alongside* the count, so deriving the count from anything but
those rows makes the two disagree inside one response with no way for a client to tell which rows the
number refers to. Fixing it properly means deciding what a blocked building should look like in the
row list - probably a third state alongside pinned/unpinned, since "someone's top-level pin is on it"
is neither - rather than only changing the total.

Until then a mobile client can advertise one more unpinned building than the dialog will offer, and
importing will report having created fewer than advertised.

## OPEN 2026-08-19: performance and ops defects found but not fixed

Found during the 2026-08-19 sweep, verified by reading both the query definition and every call
site. The three worst (`group_conversations_for` materialising every message in every group,
`_notify_group_message` loading a group's whole history on every send, and the rate limiter reading
one row four times per outbound call) were fixed in the same pass; these were not.

~~**The navbar messages dropdown builds the entire inbox to render at most 8 rows.**~~ Fixed
2026-08-19. `conversations_for` takes `only_unread`, which becomes a HAVING on the aggregate it
already computes, so the partner/last-message/identity lookups are sized by what will be shown.
`unread_conversations_for` merges that with the group half (bounded by memberships, filtered in
Python). The empty-state flag no longer needs the inbox either: `has_any_conversation` is two
`exists()` calls, where it used to test the length of the list the view had just built.

~~**The homepage runs ~12 aggregate/list queries for widgets the user may have disabled.**~~ Fixed
2026-08-19. Worth stating precisely, because most of that context was never the problem: nearly every
entry is an unevaluated queryset, so a disabled widget costs nothing already. Exactly two were eager
- the ten counts behind `home_stats`, and `home_recent_comments`, forced by the `sorted()` that
merges pin and trip comments. Both are now built only when their widget is enabled, guarded by a test
that compares actual query counts with the widgets on and off.

~~**The pin-list detail page costs two extra queries per pin (rating and wiki), unpaginated.**~~
Fixed 2026-08-19. `_list_items_with_labels` now selects `pin__location__wiki` and prefetches
`pin__reviews`. Worth noting how it hid: the function's docstring already claimed it "matches the
same prefetch shape the main map's bulk pin endpoints use ... without N+1 queries", and both model
properties involved (`Pin.rating`, `Location.display_name`) document in their own docstrings exactly
which prefetch they need. Three accurate comments, and the code between them still missed two
relations. The page remains unpaginated.

~~**Pin merge suggestion cards issue 8 `COUNT` queries each.**~~ Fixed 2026-08-19 by annotating the
four counts on `pending_merge_suggestions`, with `distinct=True` on each - the four joins multiply
one another, so plain counts would report visits x photos.

**Verified end to end 2026-08-19.** `bin/dev_env.py create` produced a working
`https://e2e-check.dev.urbanlens.org` (HTTP 200 direct and through the router), 17 healthy
containers and its own checkouts on disk; `list` reported both environments running; `destroy`
removed containers, files, registry entry and route completely. `--no-redata` skipped the REData
steps cleanly.

**RESOLVED 2026-08-21: a dev environment no longer costs a second REData stack.** A default
`create` used to start *two* stacks - nine UrbanLens containers and eight REData ones (app,
celery-worker with Playwright/Chromium, celery-beat, flaresolverr, tor, searxng, valkey, db), the
REData half alone taking roughly eight minutes on a cold image cache and then sitting idle for any
agent not working on REData integrations. The ops call has been made: `create` now takes
`redata="production" | "own" | "none"` and defaults to **production**, writing the host's
`UL_REDATA_API_URL`/`UL_REDATA_API_KEY` into the environment's `.env` and building nothing.
`--own-redata` opts back into a private stack; `--no-redata` still means neither.

The containers were never the main cost. REData exposes almost no write surfaces - most of its
"write" operations do not store what we send, they make REData go *fetch* external data about a
location - so a throwaway instance spends third-party quota pulling data that is destroyed with the
environment. Production serves most of the same calls from its cache without contacting anything,
and caches whatever is genuinely new for good. See `docs/OPS_TOOLING.md` for the modes and
`REDATA_MODES` in `bin/opslib/devenv.py` for the reasoning next to the code.

**The cold-boot fix is unexercised.** `docker compose up -d` exits non-zero when a dependent service
gives up on the app's healthcheck, and the app legitimately takes minutes on a first boot (migrate,
collectstatic, frontend build); the run then *skipped* the fifteen-minute health wait that would have
seen it succeed. That is fixed - the wait now runs whenever the app container exists - but the
verification run had warm images and `up` succeeded outright, so the new branch never fired. The
evidence for the bug is the previous run's own log ("dependency failed to start: container
ul_afb299e_app is unhealthy") followed by a healthy stack.

**`UL_CONTAINER_NAME=agent_<slug>` names nothing.** The isolation override pins every
`container_name` and `_compose` passes `-p ul-<slug>`, so both things that variable would have set
are overridden; it is a collision guard only. The comment above the start step asserted the opposite
("No -p: UL_CONTAINER_NAME ... already sets both"), and that stale comment is what `list_envs` was
written against. Both now say what is true.

**RESOLVED 2026-08-19: four ops-tooling paths that reported failure as success.**

- `dev_env.py destroy` set `containers: True` unconditionally and deleted the registry entry whatever
  `docker compose down` returned, so a failed teardown left containers running with nothing recording
  that they existed. It now reports from the exit codes, keeps the entry marked `orphaned` when the
  teardown failed, and `bin/dev_env.py` exits 1 with the compose output.
- `dev_env.py list` looked for `agent_<slug>`, a prefix nothing creates - so every environment read as
  not running. All three sites that need this name now share `devenv.container_name`.
- `dev_env.py create` wrote its registry entry before cloning and `run_step` records failures rather
  than raising, so a failed clone crashed the next step on a missing directory *after* claiming the
  environment existed. It now marks the entry failed and stops.
- The staging **data-preservation check passed vacuously**, in two independent ways: `_VERIFIED_TABLES`
  named `dashboard_pins`, which no model owns (`Pin.Meta.db_table` is `dashboard_user_pins`), and both
  database containers were addressed as `urbanlens_<UL_ENVIRONMENT>_db` while compose names them
  `urbanlens_${UL_CONTAINER_NAME:-${UL_ENVIRONMENT:-production}}_db`. Either makes a count come back
  `-1`, and the comparison skipped `-1` silently while reporting "5 tables match". An uncountable
  table is now a failure in its own right, the summary counts what was actually compared, and both
  container names come from one helper.

**And a fifth, found by the tests written for those four:** `bin/run_tests.sh` synced `src/` but not
`bin/`, while `tests/hypothesis/test_ops_tooling.py` imports `bin/opslib` directly. Every ops-tooling
test was running against whatever was baked into the image - the exact failure the script's own header
describes for `src/`, unnoticed here because the imports kept working while the code behind them
aged. The sync and both halves of the parity check now cover `bin/` too.

~~**Still open:** every dev environment is configured with a REData URL its own app container cannot
reach.~~ Fixed 2026-08-20, and it had **two** halves - the second only found by testing the first
against the live `e2e-check` environment rather than reasoning about it.

1. The URL was `http://127.0.0.1:<redata_port>`, which inside the app container is the app
   container; REData publishes on a *host* port. Confirmed live: `URLError [Errno 111] Connection
   refused` from inside `ul_e2e-check_app`. The isolation override now gives the five application
   services (`app`, `app-ws`, and the three celery services)
   `extra_hosts: host.docker.internal:host-gateway`, and the env file points at that alias.
2. Routing to it is not enough. REData's seed `.env` sets
   `RD_ALLOWED_HOSTS=localhost,127.0.0.1,redata.urbanlens.org`, so a request arriving with any other
   `Host` gets **400 DisallowedHost**. Measured: over the gateway the container got 400 where the
   host got 401, and 401 once the `Host` header was forced to `localhost`. `create` now writes
   `RD_ALLOWED_HOSTS` including the alias, and `redata_api_url`/`redata_allowed_hosts` are held to
   each other by a test.

3. And with routing and hosts both fixed, every call still came back **401**. `UL_REDATA_API_KEY` is
   seeded from the host's `.env` along with the other secrets - but it is not that kind of secret.
   It names a *row in a database*, and a private REData starts with an empty one, so the inherited
   key authenticates against nothing. `create` now mints a key in the new instance
   (`_provision_redata_key`, via `manage.py shell -c` - REData has no key-issuing command, the key
   is normally created through the admin) and writes it into the UrbanLens `.env` before that stack
   starts. Best-effort: a failure there leaves the environment usable for everything that is not
   REData, and says so in the step log.

Each failure is close to invisible on its own, and they get worse in order: connection-refused is
obviously wrong; a 400 from a service that is plainly up reads as a bad request; a 401 reads as a
credentials problem with the *host's* REData rather than as "this instance has never heard of you".

**Verified live 2026-08-20** by repairing the running `e2e-check` environment in place rather than
trusting the reasoning: from inside `ul_e2e-check_app`, `GET /capabilities/?lat=&lng=` now returns
21 domains through the authenticated gateway. That answer also validated the satellite work below -
this instance reports `mapbox`/`bing_maps`/`azure_maps` as *not* applicable (no vendor keys), so the
hardcoded list had been asking three providers it could never serve.

**One more thing that repair surfaced: recreating the `app` container 502s the stack until nginx is
restarted.** nginx resolves its upstream once, at config load, so a recreated app comes back on a new
container IP and nginx keeps dialling the old one - `connect() failed (111: Connection refused) ...
upstream: "http://172.25.0.5:8000/"` while the app itself answers 200 on `127.0.0.1:8000` and *every
container reports healthy*. `create` never hits this because it brings the whole stack up together;
any in-place repair of a running environment does, and the symptom points at the app rather than at
nginx. A `resolver`-based upstream in the nginx config would fix it properly; `docker restart
<slug>_nginx` is the one-line workaround, and is what the live environment needed.

**RESOLVED 2026-08-20: sending a group message cost a `Friendship` lookup per member, twice.**
Measured first: an 8-member send ran 18 queries, 8 of them `Friendship.between(member, sender)`. The
per-member cost was a *recorded decision* (2026-07-23) rather than an oversight - the payload carries
the sender's name, so it has to pass each recipient's own visibility, and the alternative leaks a
masked name over the live channel. What was missing was a way to ask the question in bulk.

`Profile.visible_profile_pks` batches many subjects for one viewer. A group message is the mirror:
one subject, many viewers - which that function cannot express, so both halves of a send
(`_notify_group_message` for the bell title, `broadcast_group_message` for the live payload)
resolved it a row at a time. `Profile.viewers_who_can_see(subject, viewers)` is the mirror, with
`DirectMessageTemporaryAccess.granting_viewer_pks` under it and
`identity_visibility.resolve_identity_for_viewers` on top; group create and add-members use it too.
Both directions are now held to `can_view_profile` by `test_identity_visibility_batch` across every
`VisibilityChoice` and relationship - the same treatment the original batch got, and for a sharper
reason: this one decides whether a *whole room* sees a name.

`_notify_group_message`'s docstring had promised a fixed query count since the unread check and the
preference lookup were batched. It was true of two of the three per-member things it did. There is
now a test holding it, and it counts reads only - one INSERT per notified member is the work itself.

**Same class, four more sites, fixed in the same pass:** the external API's group-member roster and
friend-ratings list, the group sidebar's last-sender previews, and global search's DM results all
resolved identity one row at a time when `visible_profile_pks` already existed for exactly that. The
sidebar one is worth naming: it *had* a query-scaling test, and the test passed, because every group
in its fixture had the **same** last sender and the function's own dedup cache hid the cost. The
test now uses distinct senders.

~~**The Building Attributes card picks the nearest building without excluding envelope parents,
ambiguous overlaps, or off-property records.**~~ Fixed 2026-08-19. `_nearest_building` now applies
`buildings_on_property`/`countable_buildings`/`confident_buildings` before ranking. Each is a
*preference*, not a hard filter: when nothing survives, the next-weakest set is ranked instead, so a
parcel whose only record is ambiguous still shows what is known rather than going blank.

~~**`ensure_building_places` ignores `parent_ref`.**~~ Fixed 2026-08-19 - nesting is resolved
topologically, with a cycle break, and a parent outside the list falls back to the parcel rather
than losing the building. The same pass found a second defect the fix exposed: `find_matching_place`
applied its mutual-centroid-containment fallback to records that *do* carry a stable provider id, so
an L-shaped block and a wing tucked into its corner could merge back into one place - undoing
exactly the reconciliation REData did to keep them apart. **Still open:** the reconciled `ref` is
persisted as a permanent identity (`Place.provider_key`, floorplan `building_ref`) and REData does
not guarantee it is stable across responses.

~~**`flatten_timeline` reads `capture_date_resolved` one nesting level too high.**~~ Not a defect on
the current tree - re-verified 2026-08-19. `_resolved_flag` reads the `attributes` blob first and
falls back to the top level, and has since commit `8bf86daf`; the finding described the code before
that.

## RESOLVED 2026-08-20: a visit suggestion named its sender even when that sender is masked everywhere else

Found by `bin/report_defect_history.py`'s incomplete-fix query, which lists fixes whose own message
implies more instances exist. Commit `1634837e` - "the calendar importer's trip invite masks identity
**like its sibling does**" - is exactly that phrase. `services/visits/visits.py` was another
instance: both branches of the visit-suggestion notification interpolated `suggested_by.username`
raw.

Same reasoning as the sibling, and it is worth restating because "they're connected, so it's fine"
is the intuition that produces this bug every time. Being connected is not sufficient permission:
`VisibilityChoice`'s own docstring notes accepted friends qualify for every level *except* `NO_ONE`.
And the message is stored as plain text, then picked up by push delivery and by
`notification_text_alerts`, which builds an SMS body from the stored text - so a name masked at
render time has already left the app.

Now routed through `_suggester_name`, with four tests. Two of them fail against the previous code
(verified by reverting), one is the anti-vacuity case, and one covers the merge-wording branch -
the two message branches are written separately, which is how they would drift again.

**Checked and deliberately left alone: the two other raw usernames near a notification write.**

- `services/social/friendship.py`'s friend-request body. Naming the requester is the *point* -
  `visibility_permits` has an `allow_pending_request` rule specifically so "asking someone to
  connect deliberately lets them see who is asking". Routing it through the resolver would be
  harmless for every visibility except `NO_ONE`, where it would produce "Member wants to be your
  friend" - an anonymous request nobody can act on. That is a product decision about whether a
  `NO_ONE` profile may send requests at all, not a masking bug.
- `services/visits/safety.py`'s community-wiki escalation. The owner opted into notifying strangers
  precisely so those strangers can look for them; masking the name would defeat the feature. It is
  in `MUTE_EXEMPT_TYPES` for the same reason.

## RESOLVED 2026-08-20: every `Friendship` status transition wrote the whole row, and could un-mute somebody

Found by `bin/report_model_writers.py`, which ranks models by how many modules write them and lists
the bare `save()` calls against each - `Friendship` came back with three. That report exists because
a bare `save()` writes *every* column from a possibly-stale in-memory instance, which is harmless on
a single-writer row and a lost update on a contested one.

`Friendship` had just become contested. The mute columns added earlier the same day are written by a
targeted `UPDATE` that deliberately leaves the instance alone (so it cannot clobber a concurrent
accept/decline, and does not move `updated`, which the profile page renders as the friendship's
"since" date). The status transitions did the opposite - `accept`/`decline`/`ignore`/`remove`/`block`
and `request` all did a bare `self.save()`. So: open someone's profile page, have them mute you in
another tab, click Remove - and their mute is gone, with nothing reporting it.

**`update_fields`, not `queryset.update()`.** The latter avoids the lost update outright and also
skips `post_save` - which the achievements system subscribes to for this model, `created_only=False`,
specifically to see a friendship *reach* `ACCEPTED` (`models.achievements.signals`). Silencing that
to fix a lost update trades one silent bug for another. There is a test asserting the signal still
fires with `status` among its `update_fields`.

**A second, worse instance in the same area.** `block_profile` re-points the row so the blocker owns
`from_profile` (deliberately - direction is the only record of who blocked whom). The mute columns
are named for the row's two *ends*, so swapping the ends without swapping them hands each person the
other's preference: A's mute of B silently becomes B's mute of A, and neither of them did it. Now
read before the swap and written with it, in one statement.

Six tests, all verified to fail against the previous code by reverting both fixes and re-running -
one of them (`test_a_request_does_not_clobber_a_mute`) passed either way and says so in its own
docstring, because `request` loads the row itself and is never stale.

**The report itself was sharpened in the same pass.** It listed every bare `save()`, including ones
on an instance the same function had just *constructed* - which is an INSERT, with no earlier load
to be stale relative to. Those buried the real findings: the `Comment` entry that looked most
alarming (8 writers) was exactly that shape, and dismissing each costs a reader the same minute.
`report_model_writers.py` now skips a `save()` on a name whose every assignment in that function is
a direct `Model(...)` call - conservatively, so one `obj = Model.objects.get(...)` anywhere in the
function and it is reported as before. That took the list from 14 flagged models to 6, and turned
`Profile`, `Comment`, `MarkupMap` and `SafetyCheckin` from false positives into clean rows.

**Also fixed, from the sharpened list:** `PinVisit`'s two visit-edit paths (pin detail and the
Memories dialog) wrote the whole row for three fields. The contending writer is `pin_merge`, which
re-points `pin` wholesale; the window is small (the POST re-fetches the visit scoped to its pin, so a
merged-away visit 404s rather than being clobbered) but `update_fields` is strictly better and costs
nothing.

**Still open:** `Label` (3), `PinList` (2), `Trip`, `TripActivity` and `SavedFilter` (1 each). Each
needs the same judgement - which columns does *another* writer touch without going through this
instance - and that judgement is per model, not mechanical. `Friendship` was worth doing first
because a second writer had just been added to it.

## 2026-08-20: bug hunt over the highest fix-density modules - 9 confirmed, 4 fixed, 5 open

`bin/report_defect_history.py` ranks files by the share of their commits that are fixes, on the
premise that where bugs have been found is where bugs are. Five parallel readers took the top of
that list (`controllers/account.py` 53%, `controllers/labels.py` 45%, `pin_restructure.py` 43%,
`saved_filters.py` 43%, `trip_activities.py` 60%), each capped at its two strongest findings, and
every finding was then handed to an adversarial verifier told to refute it and to default to
refuted when uncertain. 10 findings, 9 survived. **Each of the four fixed below was re-verified by
hand before being believed** - two of them turned out to differ from the report.

### Fixed

**Editing any trip activity that has a location returned a 500.** `resolve_activity_place` handed
its `location_uuid` value straight to a `UUIDField` filter, which raises `ValidationError` from the
ORM - and a plain view does not turn that into a 400. Confirmed by running the filter: `['"x" is
not a valid UUID.']`. The pin branch six lines above already converts with `try/except` for exactly
this reason; the location branch did not.

The report blamed the edit dialog, and it is worse than that: `location_slug` - the documented
field, named for what it holds - hit the same path, because the lookup tried `uuid=` *first* with
whatever it was given. So every caller was affected, not just the dialog. The root cause is a
naming lie: the itinerary row's attribute was `data-act-location-uuid` and had always carried the
location's **slug**. Renamed to `data-act-location-ref` on both sides, and the lookup now tries the
slug first and the uuid form only once it parses.

**The label create view stored an uploaded icon with none of the validation the edit view applies.**
No size check, no content-type check, no malware scan - while the same file posted to the edit URL
is refused with a 400. That matters more than "unvalidated upload" usually does here:
`_resize_custom_icon` deliberately returns the file untouched when PIL cannot open it (an SVG, say),
`label_icons/` is served to any authenticated user, and `MediaGateView` deletes the Content-Type so
nginx derives it from the extension. Both paths now go through one `_validated_custom_icon`, and a
test pins the call-site count so a third path cannot skip it.

**The 2FA lockout counter was read-then-write** (`attempts = (cache.get(key) or 0) + 1`), while the
two login counters directly above it use the atomic `_bump_counter` - it was left behind when they
were converted. It is the only brake on TOTP guessing for someone who already has the password.
The verifier's correction is worth keeping: the reporter claimed the lockout "never fires", which is
arithmetically wrong - a batch advances the counter by one, so the limit is still reached, just
after N x concurrency guesses instead of N. Medium, not high.

**A hidden trip activity leaked its location into the DOM.** The visible label was correctly swapped
for "Secret Location", and the real name and slug went out in the row's own data attributes and the
RSVP `aria-label`, where view-source and a screen reader both find them. Two further details found
while fixing: `effective_title` *falls back to the location's name*, so the title is itself the leak
for any activity whose author typed none; and `data-act-location-hidden` emitted the raw
`location_hidden`, so a viewer hidden by the owner's visibility *setting* was told the location was
not hidden. `build_activity_rows` now puts already-masked `display_title`/`display_location_name`/
`display_location_ref` on the row, and a test forbids the panel from mentioning `act.location.` or
`act.effective_title` at all - the leak was a template reaching past the guard, so the guard has to
be somewhere a template cannot reach past.

### Confirmed, not yet fixed

- **`merge_pins` cannot complete when the survivor is the loser's direct child** (`pin_merge.py`
  :230): `_reparent_children` skips the survivor rather than detaching it, so the survivor keeps
  `parent_pin = loser` and the loser's delete CASCADEs it away - a 500, every time, unfixable from
  the UI. Reachable through `repair_legacy_pin_coordinates`, whose `existing_pin` lookup is not
  restricted to root pins. The fix is to re-point the survivor at the loser's own parent, raising
  `PinMergeCollisionError` on a unique-constraint collision exactly as the cycle branch beside it
  already does.
- **Building-place provisioning passes REData's *unfiltered* parcel cache** (`pin_restructure.py`
  :385 and :503): the dialog filters to `buildings_on_property`, the POST does not, so off-property
  records - up to ~2,500 for a parcel inside a broad survey zone - become Places inside this
  parcel's wiki access domain. Needs a re-read of the wiki-domain consequence before fixing; the
  provisioning side was rewritten on 2026-08-19 and this entry has not been re-checked against it.
- **Smart lists evaluate a saved filter's criteria without `root_pins()`** (`pin_list_membership.py`
  :222), so a list derived from a filter includes detail pins the filter's own preview excludes -
  the same filter reports different counts in two places.
- **`prime_total_pin_counts` fetches the whole site's label-hierarchy edge table** with no filter
  (`models/labels/model.py:171`), three times per Organize page load.
- **Undoing a deleted saved filter drops its colour and opacity** - `_RESTORABLE_FIELDS` omits them,
  so the filter returns untinted. Low, but its own round-trip sweep cannot see it, because
  model_bakery leaves fields with defaults unset and the sweep therefore compares default to
  default.

### Refuted, and worth recording

One finding claimed the resend-verification page defeats its own anti-enumeration guarantee by
echoing the account's email back. The verifier established that both facts it allegedly discloses
are already disclosed to anonymous requesters deliberately, so there is nothing to leak. Recorded
so the next reader of that endpoint does not re-derive it.

## OPEN 2026-08-20: reciprocal `Friendship` rows are permitted, and "one row per pair" is only a convention

`Friendship.Meta.unique_together` is `("from_profile", "to_profile")`, which stops a duplicate in
*one* direction and permits `A->B` **and** `B->A` to both exist. Every reader assumes they cannot:
the model docstring says "there is exactly one `Friendship` row per pair", `QuerySet.between()`
matched either direction and called `.get()`, and the mute columns are per-side *of one row*.

Two ways a reciprocal pair gets created today:

- `services/import_export/import_data.py:875` creates rows directly while restoring a profile
  export; an export holding both directions restores both.
- Two simultaneous requests in opposite directions. `Friendship.request` reuses an existing row via
  `between()`, but neither caller sees the other's row before inserting, and the unique constraint
  does not cover the reversed key.

`test_calendar_sync.CalendarInviteIdentityMaskingTests` builds one deliberately, which is how this
surfaced: with mute wired into notification delivery, `between().get()` raised
`MultipleObjectsReturned` on every notification between such a pair. Before that it was quieter but
not harmless - the same raise sat behind the profile page, the friends API and
`NotificationLog.is_friend_request_pending`.

**Done 2026-08-20 (containment, not the fix):** `between()` now returns the **oldest** matching row
deterministically and logs a warning, rather than raising - a second row is data to repair, not a
reason to refuse to answer, and picking arbitrarily would make the answer depend on query planning.
`notifications_muted` does not go through `between()` at all: it asks the same predicate
`profiles_muting` does, so both mute paths read *either* row and cannot disagree.

**Not done: the constraint itself.** The real fix is a normalised pair key - a
`UniqueConstraint` on `Least(from_profile_id, to_profile_id), Greatest(...)`, or a
`CheckConstraint` forcing `from_profile_id < to_profile_id` with the direction moved to its own
column. Either needs a data migration that merges existing reciprocal pairs, and merging is not
mechanical: the two rows can hold different `status` values (one `Accepted`, one `Removed`), and
which one is right depends on history nothing records. Worth doing with a real look at production
data rather than a guess. Until then, a reciprocal pair is a warning in the logs, not an exception.

## OPEN 2026-08-19: REData consumption gaps left after this session's sweep

A full cross-repo sweep of UrbanLens's REData integration on 2026-08-19 (both repos read end to end:
REData's `api/urls.py`, every serializer, `docs/api-reference.md`, `docs/fields-available.md` and the
whole `CHANGELOG.md` `[Unreleased]` section, against all ~32 `redata_*` gateways and 8 panels).
Everything that was *wrong* was fixed in the same pass - four panels reading keys REData has never
emitted, the places gateway parsing a `{count, results}` envelope as a bare array, `?is_aerial=true`
being a parameter of a different endpoint, CRIS selecting resources that were not CRIS's, Florida's
whole sale-record provider being dropped on attribution. What follows is what was found and
deliberately **not** done, so the next pass starts from here rather than re-deriving it.

**`?limit=` is inert on every REData near-point endpoint.** `NearPointQuery`
(`REData/src/redata/api/coordinates.py`) parses `lat`/`lng`/`radius_meters`/`provider`/
`force_refresh` and nothing else; the `limit` parsing at :411 belongs to the *text*-query parser. So
every panel that passes `limit=20`/`25`/`30`/`50` caches up to REData's own server-side cap instead.
Not fixed here on purpose: trimming client-side would change the user-visible counts panels report
("N mapped within 250 m") from REData's floor to our own arbitrary bound, which is less accurate,
not more. The fix belongs in REData - have `parse_near_point_query` accept `limit` - after which the
UrbanLens side needs no change at all.

**45 of REData's 106 routes have no UrbanLens caller.** 15 are judged worth wiring up, in rough
value order: the `/street-view/` base endpoint and its two mirrored-bytes download routes (the
carousel currently hot-links provider URLs that rot), `GET /places/cid/{cid}/` plus its media
download (UrbanLens already holds the CID and throws the deep-scraped place data away),
`/parcels/{uuid}/coverage/` (the Property Records panel fires four supplementary calls per parcel
blind), four `/parks/{code}/` routes including live closure/hazard alerts, `POST /imagery/capture/`,
`/reference-documents/` near-point (Wikidata's structured heritage claims reach UrbanLens from
nowhere else), and the `land-use-areas`/`demographics`/`national-parks` parcel trio. 23 are
irrelevant by design (nested write CRUD, the readback ViewSets, IIIF).

**`GET /weather/history/` is consumed on trips and on visit history; the bulk Memories lists are
deliberately left out.** (Visit history added 2026-08-20.)

Each visit row on a pin's Visit History tab now says what the weather actually was that day -
`68° / 50°F · 1.00 in rain · gusts 31 mph` - which is the fact that makes a photograph of a flooded
basement mean something. Three things about how, since the obvious implementation is wrong in each:

- **The panel never makes the call.** The first version did, on the reasoning that the panel is
  already loaded by `hx-trigger="load"` behind a spinner. That reasoning was wrong twice over: a
  spinner does not make it acceptable for a slow REData to hold up an entire visit list for a
  decorative line of text, and it put an outbound call inside a page render, which is a thing no
  other external-data surface in this app does. Two *unrelated* tests found it, by tripping the
  suite's localhost-only network guard the moment the panel started fetching. It now reads the cache
  (`recorded_days(..., allow_fetch=False)`) and queues the gap
  (`tasks.fetch_recorded_weather`) - the same fetch-behind/render-from-cache split every pin-detail
  panel uses. Weather appears on the second view, which for this is the right trade. Days inside
  ERA5's publication lag are never queued: they are not missing, they are unanswerable until
  published, and queueing them would retry forever.
- **Sparse days are not a range.** `recorded_range` fetches `min..max` in one request - right for a
  trip's activities, wrong here: a page of visits to the same ruin can span decades, and the range
  form would fetch *and cache* every day in between to display ten. `recorded_days` clusters instead,
  merging days within a month and splitting beyond it.
- **Grouped by `Location`, not by pin.** `?children=1` lists a whole subtree, and those are different
  places; one request per location, not per visit.

**Not done, and not an oversight: the Memories timeline and Visits subpage.** Both are bulk lists
spanning a whole account - hundreds of visits across hundreds of locations - so a fetch per location
is not something a list render may do. The cache-only alternative (`allow_fetch=False`) would show
weather on whichever rows a user had happened to open the pin page for and nothing on the rest,
which reads as broken data rather than as a partial feature. Doing it properly needs a background
enrichment source that fills the cache per Location, which is a different piece of work.

The original entry, kept for the trip half: A finished
trip's weather panel was empty - the view filtered to activities scheduled today or later, so a
forecast-only panel had nothing to say about a trip that had happened. Past activities now show what
the weather actually was (`controllers.trip._build_activity_history`), fetched as one range per
location rather than one call per day, and converted to the units every other weather surface uses.
Days inside ERA5's ~6-day publication lag are never requested, so they cannot be cached as blank.
What remains is the surface the design doc named first: the shared visit dialog and Memories.
`visit_weather.recorded_weather(location, day)` is the single-day entry point for it.

**Fields fetched, cached, and never shown.** ~~`special_land_use_areas` (military installation /
correctional facility / national park / campus), `flood_zone_code`, `deed_document_links`~~ - shown
on the Property Records card as of 2026-08-20. The land-use categories are chipped rather than
listed, and ahead of "Delinquent taxes"/"Boundary available": two of the four describe ground where
being present is a different statute rather than a trespass question, which is a fact about the
*visit* and not another attribute of the property. A category present but unnamed still renders -
TIGERweb rows are confirmed to omit fields per category, and dropping the row would turn "inside a
correctional facility" into silence.

Still unread: `raw_attributes` on the parcel record (per-jurisdiction keys, no display shape that
generalises - a deliberate skip rather than an oversight).

~~The sheet thumbnail, library landing page and georeference accuracy on the historical-map
picker~~ - shown as of 2026-08-20. The thumbnail is the one that mattered: choosing between a dozen
scanned sheets of one neighbourhood is a *visual* task, and the picker offered eleven rows all
reading "Sanborn Fire Insurance Map of ...". Both the thumbnail and the catalogue page are the
**institution's** own public URLs, not REData-authenticated ones, so unlike the tile template they
need no proxy - that distinction is why they were safe to link directly and worth stating, since the
tile template two lines away must never reach the browser.

Accuracy needed a judgement rather than a field read. `rmse_meters` is the fit's own residual, and
REData's model docstring warns that a thin-plate spline interpolates its control points *by
construction*, so its residual is ~0 whatever the placement is actually like. Printing "±0 m" for one
would advertise a perfect fit for possibly the worst sheet in the list, so splines report nothing;
so does anything under 25 m, which on a scanned historical map is noise. What survives is the case
worth disclosing before somebody traces a building off the overlay: "placed to ±60 m (4 control
points)".

**Also fixed in passing: the picker's rows had no thumbnail slot at all**, so this needed the row
layout as well as the data - a fixed 2.5rem box, because the list scrolls inside a 14rem window and
one tall scan would otherwise push every other sheet out of view.

~~Entrance fees, real operating hours, directions and weather guidance on the national-park
panel~~ - shown as of 2026-08-20, via `plugins.builtin.nps.park_facts`, which the web panel and the
API payload now share (they rendered different hand-built subsets of the same payload before).

The hours case was the sharpest instance of this whole category: the template rendered "Standard
hours vary - check NPS.gov" **whenever `standardHours` was present** - that is, precisely when it
did not have to say that. Consecutive days with identical hours are now grouped
("Mon-Fri: 9:00AM - 5:00PM; Sat-Sun: Closed"), and a week NPS has only partially published renders
nothing rather than collapsing an unknown day into a range, which would read as "closed that day".

Fees needed the same care in the other direction: `cost` is a *string* in NPS's API and is sometimes
free text ("varies"), so an unparseable fee is skipped rather than guessed, and **absent fees are
not reported as free** - "Free" has to mean free. `weather_info` is deliberately still unread: it is
a paragraph of seasonal prose and the pin already has a weather panel showing the actual forecast.

**Found while doing it: the NPS panel had no stylesheet at all.** Every `nps-*` class in
`pin_nps.html` matched nothing - the card rendered with only the generic `.card` chrome. That is one
concrete instance of the "46 BEM modifiers applied in templates with no CSS rule" entry further down
this file, and it now has one. The fact grid mirrors `.simple-info-panel`'s `.simple-info-meta`
values rather than sharing the selector: hoisting that rule out of its parent changes its
specificity for every panel that uses it, which is a bigger change than this panel is worth.

**Correction 2026-08-20 to this entry's own last item.** It named "`residual_geometry` and each
source's `attributes` (the assessor's sqft/stories/condition) on the reconciled building record" as
an UrbanLens gap. Checked against REData: `BuildingRecord` promotes `name`, `address`,
`building_number` and `year_built` and nothing else - sqft, stories and condition are **not**
standardized per building, they sit in each source's raw `attributes` under whatever that county's
GIS layer calls them. Consuming them from here would mean guessing column names per jurisdiction,
which is the exact trap `cris_buildings` is stuck in. The parcel-level equivalents *are*
standardized (`BuildingCharacteristics`) and the Property Records card already shows them. So this
is a REData-side gap - promote the per-building CAMA fields there - not an unread field here.

`residual_geometry` (a parent envelope's footprint minus its polygon-bearing children) is genuinely
unread, and on inspection has no consumer worth building: it does *not* fix Place-tree overlap,
because a parent Place containing its children is correct hierarchy rather than the sibling overlap
that was the actual bug. What it would support is a map annotation for "building mass no mapped wing
accounts for", which is a real but narrow thing to want.

**Hardcoded maps that filter out new REData providers.** ~~`satellite_imagery`'s
`_REDATA_PROVIDER_NAMES` both restricts the request and gates rendering, so REData's `s2cloudless`
provider is invisible~~ - fixed 2026-08-20. ~~`cris_buildings` reads one inventory's raw column names
and so cannot show the other cultural-resource providers at all~~ - addressed 2026-08-20, and the
count was low: REData registers **25** historic inventories and UrbanLens read one.

`cris_buildings` was not the place to fix it. It renders CRIS's own raw ArcGIS columns (`USNName`,
`USNNum`, `EligibilityDesc`), so it *has* to name its provider - handing it an NRHP row blanks the
card, which is a bug that already happened once and is why the request was restricted in the first
place. Widening it would have meant either re-introducing that bug or rewriting a working NY-only
panel, its media gallery and its enrichment source.

`plugins.builtin.redata_historic_registers` is the other half instead: one card over the whole
registry, rendering only the fields REData standardizes (`name`, `resource_type`, `scope`, `status`,
`year_built`, `architectural_style`, `use_type`) and never a provider's `attributes`. It discovers
its providers from `/capabilities/` and excludes only `ny_cris`, which has the richer panel. New
`RedataCulturalResourcesGateway` keeps the `{count, complete, results, providers}` envelope that
`property_records.RedataGateway.lookup_cultural_resources` flattens away, so `RedataInfoPanelSource`'s
outage rule applies - one inventory being down must not be cached as "this place is on no register".

Two details worth keeping:

- The register **name map is not a gate**. A provider missing from it renders under a title-cased
  tag. Reading a display-name map as a permission list is precisely what hid `s2cloudless`, and the
  test says so.
- Only the kept fields are cached. `attributes`, `detail_payload` and `geometry` are per-provider,
  large, or both, and cached payloads are read on every pin-detail render.

**Still unread:** attachments outside CRIS. `nps_nrhp` can fetch a nomination *document*, and other
providers declare their own `detail_fetchable_types`; this panel is search-tier only, so those
never reach the Media gallery the way CRIS's survey photographs and inventory forms do. That is the
natural next step and a bigger one - it needs a per-provider detail fetch and a proxy route per
provider, not another panel.

The satellite half, since the fix is not simply "call capabilities":

- `s2cloudless` is **one global cloud-free Sentinel-2 mosaic per year since 2016**, delivered as a
  tile template with a `captured_on` per year. For this app's subject that is the single most useful
  source in the carousel - a yearly sequence is how you see a roof come off or a building
  disappear - and the timeline endpoint was already fetching the captures, which the carousel then
  dropped for having no entry in a dict in this repo. It was excluded with no recorded reason.
- The provider list now comes from `GET /capabilities/`; what stays written down is
  `_SHOWN_ELSEWHERE`, the handful another *UrbanLens* panel covers better (Esri's direct gateway,
  the USGS topo panel, the historical-map picker) plus the `loc_` prefix for loc.gov's scanned-map
  collections, which are generated on REData's side. A display name is looked up if we have one and
  title-cased from the tag if we do not, so a new source appears rather than being dropped.
- A capability outage falls back to the curated list rather than to nothing, unlike the
  points-of-interest panel: `/imagery/` takes an explicit provider list either way, so the fallback
  is a bounded request rather than a fan-out. **But an empty list means "all providers" at REData's
  end**, so "everything applicable belongs to another panel" has to mean *no request at all* - there
  is a test for that, because the two empty cases read identically at the call site.

**Open, found while doing it: a `tile_template` slide is one 256px tile, and the pin can be at its
edge.** `_resolve_tile_template` resolves the single tile *containing* the coordinate at zoom 15
(~1.2 km across), so a site near a tile boundary is shown in the corner of its own photograph, or
half out of frame. Tolerable for OpenTopoMap, where the slide is terrain context; wrong for
`s2cloudless` and any future tiled imagery, where the slide is supposed to be a picture of the
place. The fix is compositing a 2x2 or 3x3 block centred on the point, which is a real piece of
work (fetch, stitch, encode) rather than a parameter change.

**RESOLVED 2026-08-19: the points-of-interest registry is consumed.** It was the largest
unconsumed surface and the one most relevant to this app - agency surveillance-camera registers,
`osm_surveillance` (worldwide, and outside Chicago and Austin the only camera source there is),
`fcc_asr` antenna structures, FAA facility groups, EPA contamination programmes, storage tanks,
school layers - reachable only one provider at a time, with only `yelp` and `epa_echo` called.

`plugins.builtin.redata_site_features` now surfaces them as one "Cameras & Structures" panel. Three
things about *how*, since the obvious implementation would have been wrong:

- **No provider list is hardcoded.** Most of these providers are generated on REData's side from
  dataset tables, so their tags are not knowable to a client and a list here would silently stop
  growing. The panel asks `GET /capabilities/?lat=&lng=` which providers cover the point - a bounds
  test, no upstream call - and requests exactly those. That also answers the "capabilities is fetched
  only to render one admin card" finding: it is now on the pin-detail path, cached an hour per coarse
  coordinate, with its own rate-limit budget.
- **Failed discovery asks nothing, not everything.** A request with no `provider` fans out across the
  whole registry, which is the one outcome the capability lookup exists to prevent, so the failure
  direction had to be the safe one.
- **The two exclusion sets are kept apart.** `_SHOWN_ELSEWHERE` (providers with their own UrbanLens
  panel) is a fact about this app's UI, so a test holds every entry to a registered panel key.
  `_TOO_GENERIC` is one judgement about REData's taxonomy - `osm`'s generic point set, which would
  make the panel about nothing in particular - and is the only entry a REData change could
  invalidate. Writing them as one set hid that difference, and the test caught it.

An earlier draft of this entry said `yelp` is billable, as a reason to curate. It is not:
`billable=True` appears 11 times in REData and none are in this registry. The real cost is upstream
queries and quota, not money.


## OPEN 2026-08-19: `main` cannot start from an empty database - conflicting migrations

Found by the new dev-environment tooling on its first clean run: `bin/dev_env.py create --branch
main` builds the stack, and the app container dies during init with

```
CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph:
(0002_v0_4_0b0, 0006_v0_4_0_indexes in dashboard).
```

This is a property of the branch, not of the tooling - the environment was correctly isolated
(`ul_<slug>_*` containers, own database) and every other step passed. Any deploy of `main` against a
fresh database fails the same way; existing databases are unaffected, because `migrate` only walks
the graph when it has work to do, which is why nothing has noticed.

The fix is a merge migration (`makemigrations --merge`) on `main`, or removing whichever leaf is
redundant. Worth checking before the next release branches off it.

`bin/check_migration_graph.py` **does** catch it - pointed at main's tree it reports exactly this,
naming both leaves. The check simply postdates `main`: that file does not exist on that branch, and
pre-commit only runs what the checked-out branch carries. So this is not a gap in the check; it is a
branch that has not received it yet, and merging forward is enough to stop it recurring.

(An earlier draft of this entry claimed the checker lacked a leaf check. That was wrong - verified by
running it against the cloned main checkout.)

## ⚠ Dev environment `devs1` is down - read this before restarting anything (2026-08-14)

Four entries below describe one situation. They were filed in discovery order; this is the order
they must be *acted* on, because fixing the visible problem first breaks something currently healthy.

1. **Snapshot the database.** Three of the pending migrations carry data
   (`0027_places_backfill`, `0039_encrypt_contact_and_note_fields`, `0042_label_merge_duplicates`).
2. **`manage.py migrate`** - the dev DB is **18 migrations behind** (`0026`-`0043`). See
   *"the dev database is 18 migrations behind the code"*.
3. **Then** `docker compose restart app`. Not before: Celery workers do not autoreload, so they are
   currently running old code that matches the old schema and are **healthy**. A restart makes them
   load current code against a stale database.
4. **Keep the `chown` on every `docker cp`** - see *"the documented `docker cp` resync breaks the
   app container"*. Without it the app cannot write `logs/django.log`, Django's logging config
   raises, and `runserver` dies before binding port 8000. The ownership has been repaired once
   already; the next unguarded resync undoes it.

Why this needed a summary: the underlying drift was recorded in `CLAUDE.local.md` on 2026-08-06 as a
*stale-files* problem and went unrecognised as a *database* problem for eight days. The information
was never missing - it was filed under a heading nobody would search when the site stopped serving.

---
Each entry should have enough detail (repro steps, file:line, symptoms) for a future session
to pick up without re-discovering the problem from scratch.

## OPEN 2026-08-15: frontend TypeScript audit - remaining findings

Full-tree audit of `dashboard/frontend/ts/` (every file read, eight passes). The four
security/safety items were fixed in the same pass; everything below was found but **not** fixed.
Line numbers are as of 2026-08-15 and will drift.

**Fixed 2026-08-19** (strike these when reading the list below):

- The one-shot `AbortController` in `entries/photo-location-scan.ts`. Every scan now begins with a
  fresh controller and cleared hits/clusters/selection (`beginScanState`), so the Firefox/Safari
  `webkitdirectory` path survives a Stop and a re-scan no longer double-counts photos into
  clusters. The directory-walk path passes `alreadyBegun` so the walk and Stop stay on *one*
  controller - resetting twice would have made Stop silently do nothing.
- The missing `pointercancel` in `shared/map-image-overlays.ts`. An interrupted touch drag on an
  overlay corner left `map.dragging` disabled until reload; `pointercancel` and
  `lostpointercapture` now run the same release handler, which is idempotent.
- The dead people-label Merge button (`_organize_label_card.html`, `peopleMergeSingle`). Note the
  fix is **not** the one the entry implies: wiring it to `label.merge` would 404, because
  `KIND_USER` and `KIND_MEDIA` both set `enable_single_merge=False`. The control was for a
  capability the server refuses, so it is gone. Guarded by
  `tests/hypothesis/test_label_card_merge_affordance.py`.

Also **not** a defect, checked 2026-08-19: `flatten_timeline`'s `capture_date_resolved` read. A
later report claimed it looked at the wrong nesting level; `_resolved_flag`
(`services/locations/imagery_timeline.py`) already checks `attributes` first and the top level
second, and has since commit `8bf86daf`.

**Highest-value single change:** ~40 raw `fetch()` call sites bypass `shared/fetch-json.ts`
(`fetchJson`/`sendJson`), several with no `response.ok` check at all, so a non-2xx dies in a
`void`-ed promise with no toast. Six hand-rolled wrappers exist beside it: `postForm`/`getJson`
(triplicated across the three games), `postForHtml` (organize-tab-manager), `postJson`
(album-items), `savePosition` (album-map). Migrating them is mechanical, adds timeouts (several
uploads can currently hang forever), and converts the dominant silent-failure mode into the
required toast-on-error behaviour. Two deliberate exceptions to keep: `webauthn-client.ts`
(self-contained for the minimal auth layout, already ok-checked) and the two E2EE calls that need
raw `Response` semantics (201-vs-200, `redirected`).

**Correctness, user-visible:**

- ~~`entries/map-annotations.ts:2264` - right-click-to-delete-vertex is dead code:
  `m.on("contextmenu.rcdelete" as never, ...)` is jQuery-style event namespacing that Leaflet does
  not support, so it binds a literal event name that never fires - while the toast at :2338 tells
  the user to right-click. The `as never` casts were the compiler flagging exactly this.~~ Fixed
  2026-08-22: binds the real `"contextmenu"` event instead.
- ~~`entries/map-annotations.ts:1371` - `loadDetailPins` has no ok-check and **clears the existing
  pin layer and list** on failure (console.warn only). :2558 `flushDpAutoSave` swallows validation
  errors, so autosaved edits are silently lost. :2047 `placeMediaItemAt` has no ok-check~~ ...
  :1643/:1712 bulk promote/delete `Promise.all` paths have no `.catch`, so one failure is an
  unhandled rejection with the selection never cleared. Fixed 2026-08-22, except the last part was
  already half done: `doDeleteSelectedDp` (the `:1712` delete path) already had its `.catch(() =>
  false)` from an earlier, unrelated change - only `doPromoteSelectedDp` (`:1643`) still needed it,
  and now has the identical fix with a comment pointing at its already-fixed sibling. **Still
  open**: `placeMediaItemAt` still has no loading indicator for the server-side image-materialize
  step it waits on - not attempted here since `window.mediaApplyMaterializedDrop`'s own contract
  (defined in the gallery/organize module) would need to be understood first, and this file has no
  established loading-state convention for the drag-and-drop-onto-map interaction to reuse.
- `entries/photo-location-scan.ts:207` - the `webkitdirectory` fallback path (Firefox/Safari)
  reuses an already-aborted `AbortController`, so after one Stop click **every** later scan halts
  on the first file. Also: hits accumulate across scans (re-scanning double-counts into clusters),
  and the photo uploads that run *after* the "Uploaded" toast have no progress indicator.
- ~~`shared/map-export.ts:270` + `themes/base.html:816` - `download()` awaits tile fetches (up to 8s
  each) but no caller awaits it: no spinner, no toast, unhandled rejections, and the save flow
  closes the composer mid-export so shapes project against a map being torn down.~~ Fixed
  2026-08-22: all three call sites in `base.html` now await the promise, toast on failure, and
  disable their trigger for the duration (the read-only viewer's button gets the real `.is-loading`
  spinner since it already has `.btn`; the composer's own bespoke `cmc-download-btn` just disables,
  since it doesn't participate in that class and this fix didn't attempt to move it onto one). The
  save flow now chains `_closeComposer()` onto the download's own completion instead of firing the
  download and closing the composer in the same tick - it was the composer's `deactivate()`
  tearing down `_composerMap` itself while `download()` was still awaiting tile fetches from it,
  exactly contradicting the comment already in that code explaining why the close had to wait.
- ~~`shared/markup-toolbar.ts:748` - `flushMarkupAutoSave` never checks `r.ok`, so a 400 (e.g.
  over-long label) reports success; the single pending-save slot also means editing item A then B
  inside the 500ms debounce silently discards A's changes~~ Fixed 2026-08-22: added the ok-check,
  and replaced the single shared slot with a per-item-uuid map, since it turned out reachable
  without even editing two items in sequence - `setItemLayer` (the sidebar's inline layer picker)
  shares the same autosave path and can target a completely different item than whatever the edit
  panel has open. **Still open**: nothing flushes pending autosaves on unload/tab-close - a
  `beforeunload` handler can't reliably await an in-flight `fetch`, and this app's CSRF header
  doesn't fit `navigator.sendBeacon`'s simple-request shape, so that half needs its own dedicated
  pass rather than a quick addition here.
- `entries/organize.ts:106,311` - the Media tab is **fully dead UI**: the template renders it
  selectable with checkboxes, a filter bar and Edit buttons, but no `OrgTabManager` is built for
  it, `ORG_FILTER_NAMESPACES`/`TAB_FILTER_NS` omit it, and the consolidated dialog opener has no
  `media-label-edit-dialog-body` case, so Edit swaps a form into a dialog nothing opens.
  Separately, `_organize_label_card.html:77` references `peopleMergeSingle`, which is defined
  nowhere in the codebase.
- ~~`shared/organize-filter-engine.ts:188` - `countVisibleCards` tests `card.style.display`, but
  tree view sets `display` on the `.tag-tree-item` *wrapper*, so cross-tab match counts and the
  "N categories also match" footer count every card as visible. It duplicates `getOrgVisibleCards`
  (:99), which gets it right.~~ Fixed 2026-08-22: `countVisibleCards` now delegates to
  `getOrgVisibleCards` instead of re-deriving the check.
- `shared/map-image-overlays.ts:209` - corner drag never handles `pointercancel`; an interrupted
  touch gesture leaves `map.dragging` disabled permanently.
- ~~`entries/spotguessr.ts:1491` - `submitGuess` has no in-flight guard, so a double-click posts
  twice and double-counts the session score~~ Fixed 2026-08-22: disables the submit button for the
  duration of the request, re-enabling only on failure. **Still open**: `:840 reportRoundTimeout`
  has no error handling, so a failed timeout POST hangs the round forever; all three games silently
  null the WebSocket on close with no reconnect and no "connection lost" notice.
- ~~`entries/trivia.ts:856` and `entries/consensus.ts:1051` - missing the round-id guard spotguessr
  has (`lastRevealedRoundId`), so the last player to answer double-counts HUD points.~~ Fixed
  2026-08-22, differently in each game because their reveal broadcasts aren't the same shape:
  trivia.ts gets a `lastRevealedRoundId` guard matching spotguessr's, since `submitAnswer`'s own
  response can independently credit the same round `showBroadcastReveal` will also see.
  consensus.ts needed a **resolution-aware** guard instead
  (`{roundId, resolution}`, not just `roundId`) - `services/consensus/session.py`'s competitive-round
  disagreement sub-phase broadcasts `round.revealed` for the *same* round_id twice by design (once
  `vote_open` with zero points, again once the tiebreak vote resolves with the real ones), so a
  bare round-id guard would have silently discarded every vote-winner's actual points instead of
  fixing anything.
- ~~`shared/organize-priority.ts:69` and `shared/album-items.ts:118` - optimistic reorder with no
  rollback on failure~~, so a failed save left the DOM showing an order the server never actually
  got. Partially fixed 2026-08-22: both now capture the order at drag-start (also covers the
  priority list's non-drag reorder paths - the order-editor and the top/bottom jump buttons) and
  restore it if the save fails. **The "no save sequencing" half is still open, and is not just a
  missing debounce**: since each save POSTs the *whole* order rather than a delta, chaining saves
  so they reach the server one-at-a-time interacts badly with the rollback above - if an earlier
  queued save fails and reverts to *its* pre-drag order, a later save that already succeeded would,
  on its own turn in the chain, read that just-reverted DOM and re-persist the stale order as if it
  were correct. Fixing this properly needs either a monotonic version per save (reject/ignore a
  write older than what the server has) or reworking rollback to fall through to the *next* known
  order rather than always "what preceded this specific drag" - either way, real design work, not
  a quick addition.
- ~~`shared/confirm-dialog.ts:90` - re-entrancy: opening a second dialog while one is open
  overwrites `resolveCurrent` (first promise pends forever) and `showModal()` on an open dialog
  throws into the promise executor.~~ Fixed 2026-08-22: a call while the dialog is already open
  now settles the earlier one as cancelled first, the same as a backdrop click would.
- ~~`shared/scroll-to-hash.ts:50` - re-scrolls on *every* `htmx:afterSettle` for the page's life, so
  any later swap yanks the reader back to the original anchor.~~ Fixed 2026-08-22: remembers the
  hash it already scrolled to and only re-arms if the hash itself changes.
- ~~`shared/onboarding-tour.ts:87` - auto-dismiss hooks bind only to elements present at init; HTMX
  swaps orphan them, so dismissed cards reappear.~~ Fixed 2026-08-22: re-runs registration on every
  `htmx:afterSettle` (a `WeakSet` keeps that idempotent rather than stacking a second listener onto
  an element that survived the swap unchanged). Found and fixed the same fix's own prerequisite bug
  while here: the doc comment says `retryEvent` fires *in addition to* `htmx:afterSettle`, but the
  code was an `if/else` between them - Organize's own `retryEvent` (a tab-switch, not an HTMX event)
  meant that page never listened to `htmx:afterSettle` at all, so re-registration would have gone
  in but never actually run for any HTMX-driven update there.
- ~~`shared/organize-header.ts:113` - a transient window resize below 768px *permanently* overwrites
  the stored gallery view preference.~~ Fixed 2026-08-22: the mobile fallback is now purely a
  display-time computation (`effectiveView()`) layered over the stored preference rather than a
  call to `setSharedView()` that persisted "list" over it - widening back past the breakpoint
  restores "gallery" automatically since the stored value was never actually touched.
- `entries/article-wysiwyg.ts:532` - the first WYSIWYG keystroke re-serializes the whole article
  through a lossy `tiptap-markdown` parse (`html: false`), rewriting content document-wide, not
  just at the edit point. Needs round-trip tests over real saved articles before it is trusted.
- `shared/e2ee-client.ts:238` - the `e2ee-busy` class it sets during login has **no CSS rule
  anywhere**, so the ~1s synchronous Argon2id derivation shows no indicator at all; the unlock
  dialog (:682) has no busy state either, while the reset dialog next to it does it correctly.
- `shared/e2ee-client.ts:1326` - retry storm: a thread with an unreadable key re-fetches the same
  conversation/group key once per message (50 sequential identical failing requests on a
  50-message thread). :1459 `decryptDom` also strips `data-e2ee-*` *before* attempting decryption,
  so a transient failure is permanently unrecoverable on WS-appended messages.
- E2EE keys persist in IndexedDB across logout - `clearProfileKeys` is called only from
  `resetKeys`. Possibly intended (documented same-origin trust boundary), but the logout gap looks
  unconsidered rather than chosen; decide it explicitly and add a "forget this device" action.

**Operational:**

- `shared/location-search-engine.ts:140,197,916` - three direct browser-to-Nominatim calls bypass
  the server-side rate limiter and cost tracking and violate Nominatim's usage policy. The file's
  own comment already flags this as a KNOWN GAP. Needs the server-side geocode proxy (mirroring
  the Google Places one), which would also enable one aggregated suggestion endpoint.

**Structural (no user-visible symptom):**

- The three games triplicate ~1,500 lines of session/lobby/chat/invite/fetch plumbing (19 blocks
  differing only by an `sg-`/`cs-`/`trivia-` prefix). Extracting `game-net` / `game-session` /
  `game-friends` / `score-rows` removes ~1,000 net lines and makes the next game cost ~500 lines
  instead of ~1,300.
- `entries/map-annotations.ts` is eight features in one 2,645-line `init()` closure. Three
  extractions are nearly free today: rectangle-select (already generic, zero closure deps), the
  satellite/street-view carousel twins (95% identical), and the building-import dialog.
- `shared/e2ee-client.ts` (1,537 lines) mixes key-lifecycle service code with three hand-built
  `innerHTML` dialogs; the crypto/store layering beneath it is clean and should not be disturbed.
- Five picker widgets reimplement the same dropdown mechanics (four different blur-close timeouts,
  three chip implementations, five `escapeHtml` variants, keyboard nav missing entirely from
  `createChipPicker` and `label-rel-picker`). Note these files are otherwise **correctly**
  HTMX-shaped - the server renders their option lists and TS only filters - so do not "fix" them
  by adding round trips.
- Organize's five modules communicate over four channels at once (imports, 13 window globals with
  last-writer-wins handler slots, CustomEvents, an htmx response header), and the kind/ns/tab
  vocabulary is encoded in five separate places. It also runs a private copy of the shared
  `window.ulBulkToolbar` that `static/js/bulk-toolbar.js` says it mirrors.
- `shared/map-layers.ts:198` has no `destroy()`, so document/matchMedia/map listeners accumulate
  on per-dialog maps (the comment-map composer). `shared/photo-map.ts:204` is the model.
- Test coverage is inverted: all test files cover the small shared modules; the four largest files
  (`map-annotations`, `spotguessr`, `e2ee-client`, `consensus`) had zero until this pass added
  `e2ee-client.test.ts`/`e2ee-store.test.ts` (see `ts/testing/fake-indexeddb.ts` - happy-dom has
  no IndexedDB, which is why the store was untested).

**HTMX opportunities** (per the HTMX-first rule) - roughly 1,500-2,000 lines of DOM templating
that the server could render: game lobby lists/summaries/friend pickers, map-annotations' detail
sidebar + photo panel + bulk-edit dialog + `doSendSelectedDpToWiki` (which hand-parses
`HX-Trigger` over raw fetch - it is hand-rolled htmx already), organize's merge dialog (a third
copy of card rendering the server owns) and `postForHtml`/`replaceRows` (a hand-rolled
`hx-post`+`hx-swap`), album add/remove (two round trips where sibling flows do one `hx-post`), the
article live preview (already POSTs to a server render endpoint), and the three E2EE dialog shells.

**Out of scope but larger than all of the above:** 21,378 lines of untyped, untested, unlinted
inline JavaScript across 131 template `<script>` blocks - `map/index.html` alone is 5,152 lines
(and holds the pin-cache *writer*), `messages/index.html` 1,771, `trips/detail.html` 1,375,
`location/index.html` 1,284, `base.html` 1,116. Several audited bugs sit on the inline side of a
TS/template seam; that is where bugs collect, because the types stop there.

## NOTE 2026-08-11: do not naively wrap `PinShareCreateView.post` in `transaction.atomic`

`ATOMIC_REQUESTS` is unset, so views run in autocommit. `controllers/pin_sharing.py:137` performs
a related sequence - stamp the pin's origin share, create the `PinShare`, `share.images.set(...)`,
`record_share_exposure(share)`, optionally share the attached markup map, then create child-pin
shares - with no transaction. A partial failure can leave a `PinShare` with no `LocationExposure`,
which is the provenance invariant `CLAUDE.md` calls out.

**Wrapping the view in `atomic()` would make this worse, not better.**
`share_provenance.record_share_exposure` deliberately catches `DatabaseError`, logs, and returns
None (`share_provenance.py:119`) so a bookkeeping failure doesn't fail the user's share. Inside an
`atomic()` block Django marks the transaction broken as soon as a `DatabaseError` occurs, and every
subsequent query raises `TransactionManagementError` - so the naive fix converts a tolerated,
logged degradation into a hard 500 on the share itself, and takes the markup-map and child-pin
shares down with it.

If atomicity is wanted here, the swallow has to move inside its own nested `atomic()` first, so
the savepoint absorbs the error and the outer transaction survives. Recording this because
"multi-write view with no transaction" looks like an obvious omission and is not.

The same audit over all of `controllers/` flagged 18 methods with 3+ direct writes and no
transaction, but the count is inflated by dispatchers: the top hit
(`controllers/settings.py:159`, "18 writes") is a 15-branch `if/elif` on `section` where exactly
one branch runs. Only paths whose writes share an invariant are worth looking at.

## Coverage note (not a defect): 20 of 32 notification types have no per-type delivery control

Measured 2026-08-11: 32 `NotificationType` values, 13 preference stems, 12 of which match a type.
The uncovered 20 include `safety_ci_due`, `safety_ci_overdue`, `pin_import_complete`,
`friend_suggestion`, `spotguessr_invite`, `trivia_invite`, `consensus_invite`, `map_shared`,
`ai_extraction` and the generic `error`/`warning`/`info`.

This is deliberate and documented in `preference_field_names()`'s docstring ("Callers must expose
exactly these and must not invent defaults for the types that are missing"), and some of them -
the safety escalation chain in particular - are arguably *right* to be non-silenceable. Recorded
only so the gap is visible when someone asks why a given notification has no setting.

## OPEN 2026-07-27: ~46 pre-existing test failures on `feature/external-api-mobile-v2` (baseline-verified)

A broad sweep (`-k "pin or wiki or location or boundary or import or share or detail or merge or
restructure or undo or map"`) over `src/urbanlens/dashboard/tests` gives **46 failed, 3484 passed**.
None are regressions from the Place-consolidation phase-0 work: the four files whose failures
could plausibly have been caused by it were run with that change set `git stash`ed and again with
it applied, giving **byte-identical results both ways (12 failed, 117 passed, same test IDs)**.

**33 of the 46 are now FIXED** (2026-07-27, same session). Two patterns dominate:
*tests that depend on ambient machine state or on an implementation shape that has since changed*
(1-6, 11-14 below - they pass on a bare CI box with no credentials and fail on a dev box that has
them, or vice versa), and *test-harness behavior leaking into the thing under test* (7-8). Only two
of the 33 turned out to be product bugs (9-10).

Fixed:

1. `test_legacy_cid_coordinate_fix.py::RepairLegacyPinCoordinatesTests` (7) - the helper's
   `location.cid = ...` goes through `Location.cid`'s setter, which calls `GooglePlaceService`
   with `fetch_if_missing=True` and hits REData's nearby-places search live. Now calls
   `set_cid_for_entity(..., fetch_if_missing=False)`, the service's own bulk-path flag. **Note the
   sharp edge that caused this: assigning a plain model attribute performs synchronous network
   I/O.** Worth revisiting on its own merits.
2. `test_websocket_auth.py` (1) - not a consumer bug at all. Tests ran Django's default PBKDF2
   (~1.2M iterations) because nothing overrode `PASSWORD_HASHERS`; hashing inside the connection
   handshake exceeded `WebsocketCommunicator.connect()`'s 1-second default. `settings/test.py` now
   sets MD5, which also speeds up every test that bakes a User.
3. `test_pin_redata_media_proxy.py` (2) - asserted "unconfigured gateway returns 404 not 500" while
   *assuming* the machine had no REData credentials. With credentials present the gateway built
   fine, made a real call, and died on a DB write from a `SimpleTestCase`. Now forces the
   unconfigured state by patching `__post_init__`.
4. `test_flickr_album_import.py` (1) - same shape: every sibling patches `flickr_is_configured`,
   this one didn't, so it saw "not configured" and never reached the blank-URL branch.
5. `test_property_records_plugin.py` (1) - assigned to `Location.address`, which is a read-only
   composed property; now sets the component fields.
6. `test_pin_model_extra.py::PinEffectiveColorTests` (2) - test/implementation drift.
   `icon_source_label()` sorts in Python via `sorted(self.labels.exclude(kind="user"))` and no
   longer calls `.order_by()`, but the mock still stubbed `exclude().order_by()`. The real call
   iterated a bare MagicMock and got nothing, so the expects-a-colour cases failed and the
   expects-None cases passed **without exercising anything**.

**A further 19 across ten files are now also FIXED** (2026-07-27, same session). That set was
previously listed here as "genuine failures reproducible in isolation" - **that label was wrong for
more than half of them**, and the correction is the useful part. Those ten files now give
**344 passed, 0 failed**. Two systemic causes accounted for twelve:

7. **`@given` + a row-writing `setUp` leaked rows across an entire test class** (10 failures:
   `RoundTripCommentsTests` 8, `ArbitraryChainDepthPropertyTests` 2). `hypothesis.extra.django`'s
   mixin routes `@given` tests through `unittest.TestCase.__call__`, bypassing Django's
   `_pre_setup`/`_post_teardown` wrapper; hypothesis instead calls those per *example*. `setUp` is
   still called once by `unittest`'s `run()` - before the first example, so **outside every
   per-example transaction**. Its rows landed in the class-level atomic and survived to
   `tearDownClass`, so the *next* test in the class died in its own `setUp` on
   `dashboard_locations_latitude_longitude_uniq`. Fixed once for the whole repo in
   `core/tests/testcase.py`: `TestCase` now defers `setUp`/`tearDown` (and drains cleanups) into
   `setup_example`/`teardown_example`. **Any class mixing a `@given` test with a row-writing
   `setUp` was affected**, which is most of `tests/hypothesis/`.
8. **`UL_CELERY_TASK_ALWAYS_EAGER=True` turns "dispatched to a worker" into "ran inline"** (2
   failures). Needed for local non-Docker pytest, but it silently invalidates any test asserting
   that a request *didn't* do background work.
   - `test_direct_messages.py::...::test_second_message_in_same_streak_is_debounced` -
     `create_direct_message` schedules the alert task, which ran eagerly *outside* the test's patch
     and claimed the debounce marker, so both explicit calls were no-ops.
   - `test_location_place_name_lazy.py::...::test_page_render_never_calls_the_live_resolver` - the
     view correctly dispatches `resolve_location_place_name`; eager mode then resolved *during the
     request*, which is exactly what the test forbids. It was **order-dependent, not
     isolation-clean**: it passed when the whole file ran (the preceding class masked it) and
     failed when run alone.
   Both now stub `safely_enqueue_task` so they measure the request path, which is the actual claim.

Two were **real product bugs**, both fixed:

9. `services/map_pin_share_detection.arrow_points_toward` returned a garbage answer when a pin sat
   on an arrow's tail. The boundary centroid lands ~1e-14 degrees off the tail through ordinary
   float error, and `bearing_degrees` turns that ~1-nanometre displacement into a confident angle -
   measured at `106.29` vs the arrow's own `89.36`, inside the 35-degree tolerance. An arrow drawn
   *from* a pin and pointing away therefore recorded a **DETECTED `PinShare` the sender never
   intended**. Now guarded by `_DEGENERATE_TAIL_SEPARATION_DEGREES` (1e-7, far below the 1e-6
   coordinate storage precision), with a property test over arrow headings.
10. Creating a pin through the map's add-pin dialog never set `name_is_user_provided`, so a
    hand-typed name was eligible for the `tasks.upgrade_placeholder_pin_names` sweep that clears
    non-user-provided names - despite that task's own docstring defining the flag as "a user
    actually typed something". `create_pin_for_profile` now takes an explicit
    `name_is_user_provided` (default False, preserving importer/offline-sync semantics) and
    `maps.post_add_pin` passes it. **Left deliberately unchanged:** the external API's pin-create
    still defaults to False, so a name typed in the mobile app is not protected until edited -
    inconsistent with its own PATCH path (`external_api/views.py:746`), and worth a decision.

The remaining five were test bugs of one shape: **substring assertions against a whole page, where
the string also appears inside the page's own inline `<script>`**.

11. `test_pin_edit_controller.py::PinDescriptionEditableTests` (2) - asserted description *markup*
    against the full page, but `#pin-overview` is `hx-get`-loaded, so the markup is only in the
    partial. Both classes' docstrings already documented this split. The assertions moved to
    `PinOverviewEditableDescriptionTests`, which renders the partial. Note
    `assertNotIn("pin-description--empty", full_page)` could **never** pass - the click-to-edit
    script toggles that class by name.
12. `test_profile_hero_meta_editable.py` (2) - same thing; the wiring script builds
    `>Add when you started exploring...</span>` as a string literal, so even the `>...<` idiom the
    file already used elsewhere was insufficient. Now strips `<script>` blocks and asserts against
    the markup.
13. `test_trip_controller.py::...::test_outsider_gets_404_indistinguishable_from_a_missing_trip` -
    compared two responses byte-for-byte including the CSRF token. Django re-masks the token per
    call, so a page holds several *different* strings for one secret and no two renders ever match.
    Now normalizes token-shaped runs before comparing.
14. `test_pin_media_endpoints.py` (1) - a bare `mock.Mock()` has a truthy `is_redirect`, sending
    `fetch_with_revalidated_redirects` down its redirect branch and handing `urljoin` a Mock
    (`TypeError` -> 500). The sibling `test_media_materialize._ok_response` sets it correctly. Also
    removed the test's real-DNS dependency.

15. **`test_external_api_wiki_oracle.py::WikiDiscoveryOracleTests` was 429ing, not 404ing** (12
    SUBFAILEDs). An earlier revision of this file claimed it "passes cleanly in isolation" -
    **that was wrong**; it fails alone too. The class walks all 24 `WIKI_ROUTES` once per
    invisibility case over a single credential (72 requests), which blows past
    `ExternalApiBurstThrottle`, so the tail of the list came back **429 instead of 404**. The four
    routes at the end of `WIKI_ROUTES` are the comment writes, which is why the failures looked
    suspiciously like a comments-specific security hole. It was not one: all three cases returned
    429 *identically*, so the anti-enumeration property held throughout. `setUp` now calls
    `disable_throttling(self)`.

    **The part worth keeping:** because those four routes never got past the throttle, the oracle
    guarantee for `POST comments/`, `DELETE comments/1/` and both reaction routes was **silently
    unverified** - the test looked like it covered them and did not. It now genuinely does, and
    they pass. When a sub-resource is appended to `WIKI_ROUTES`, check it is actually reached.

Measured on `-k "external_api or api_key"` (2026-07-27): **59 failed / 668 passed** before this
session's fixes, **12 failed / 715 passed** once causes 7-8 landed, and **715 passed / 435 subtests
passed / 0 failed** once 15 did. Note the original 59-failure figure was only partially itemized at
the time: the capture had been truncated by a `Select-Object` filter, so ~45 of those were never
inspected individually; they stopped failing across causes 7-8.

Still open:

- The friend-invite privacy cluster documented below (7).

Reproduce a baseline cheaply with `pytest <files> -q --reuse-db` after `git stash`, rather than
re-running the whole 41-minute sweep. Set `UL_TEST_DB_NAME` to something unique per agent and
`UL_CELERY_TASK_ALWAYS_EAGER=True` (see cause 8 for what that costs you).

## Historical detail from the original 2026-07-26 report

Found while building the external-API social domain. **Not caused by that work** - verified by
reverting `controllers/friendship.py` and `controllers/notifications.py` to `f529b0f4` and
re-running the identical selection: 9 failures before the change, and the same 9 after.

```
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_existing_user_actually_receives_friend_request
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_gmail_variant_of_existing_email_is_matched
test_friend_invite_privacy.py::InviteByEmailPrivacyTests::test_response_identical_regardless_of_target_friend_request_visibility
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_pending_cards_are_structurally_identical_across_kinds
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_pending_cards_carry_no_type_revealing_urls_or_ids
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_registered_and_unregistered_pending_entries_render_identically
test_friend_invite_privacy.py::OutgoingRequestWidgetPrivacyTests::test_registered_target_identity_is_hidden_in_the_pending_widget
test_friend_request_message.py::EmailInviteMessageTests::test_message_is_stored_on_the_friendship_for_an_existing_user
test_external_api.py::PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid
```

The eight friend-invite ones share a likely root cause: the invite path now gates the
registered-account branch on `Profile.visibility_permits(to_profile.friend_request_visibility,
to_profile, inviter)` (a deliberate security fix - a bare `!= NO_ONE` check previously let
anyone who knew an address bypass a restricted visibility setting). `friend_request_visibility`
defaults to `ANYTHING_IN_COMMON`, and a freshly-baked target profile has no pin/friend/trip in
common with the inviter, so the gate now correctly refuses - but these tests still assert that a
`Friendship` row *is* created. The tests appear to predate the gate and were never updated;
they need to set the target's `friend_request_visibility` to `ANYONE` (or establish something in
common) in setUp.

`PinSyncViewTests::test_child_pins_are_served_with_their_parent_uuid` is unrelated and fails
with `PinCreationError: You already have a pin at this location.` - it looks like the test
creates two pins at coordinates that resolve to the same `Location`.

Whoever picks this up should confirm the intended behaviour before editing the assertions:
if the gate is right, the tests are stale; if the tests encode a real product requirement
("an emailed invite should reach anyone regardless of their visibility setting"), then the
gate needs a documented exception instead.

## Full-codebase audit (2026-07-25): curated high-severity findings

A systematic full-codebase audit (every model/controller/service/template/TS/SCSS file, all
migrations, the full test suite) ran 2026-07-25, tracked in `docs/notes/ai/codebase-audit.md` (35
units, full findings with file:line references for every bug/inefficiency/improvement found — see
that doc for anything not listed here, including all "improvement"-grade and maintainability
findings). This section curates only the highest-severity/highest-impact items into this file's
convention; the full ranked list per feature area is more complete.

**SSRF: Immich server URL is user-controlled with no private-IP/scheme guard.**
`models/immich/model.py:83` (`ImmichAccount.server_url`, via `forms/immich_form.py`) accepts any
well-formed URL with no loopback/private/link-local restriction. `controllers/immich.py:108-110`
(`ImmichSettingsView.post`) pings it server-side before saving, and later flows
(`get_map_markers`/`get_asset_thumbnail`/`get_asset_original`) proxy the response back to the
browser. Any authenticated user can point `server_url` at internal infrastructure
(`http://169.254.169.254/`, `http://localhost:6379/`, an internal admin panel) and use the
ping/thumbnail endpoints as an authenticated blind-SSRF oracle. Self-hosted-by-design feature, so
risk is single-tenant, but worth a scheme/private-IP guard in `clean_server_url`.

**SSRF: `services/security/url_safety.py`'s IP blocklist misses RFC 6598 CGNAT space.**
`is_blocked_address` (`url_safety.py:28-22`) checks `is_private`/`is_loopback`/`is_link_local`/
`is_reserved`/`is_multicast` but not the `100.64.0.0/10` Carrier-Grade-NAT range — verified
`ipaddress.ip_address("100.64.0.5")` returns `False` for every one of those checks. Many cloud
providers route internal-only infra (AWS NAT gateways, GCP internal LBs) through this range, so a
user-supplied URL resolving there sails through `ensure_public_http_url` unblocked. This is the
*only* IP-range guard for AI link extraction (`services/ai/link_extraction.py`), pin-suggestion
photo download (`services/pins/pin_suggestions.py`), and media materialization
(`services/media/media_materialize.py`) — one missed CIDR range is a gap in three subsystems at once.

**Decompression-bomb protection in the full-archive importer only checks forgeable declared
sizes.** `services/import_export/import_data.py:290-296` sums each ZIP member's *declared* `file_size` against a
ceiling, then calls `zf.extractall()` unbounded. `file_size` in ZIP headers is attacker-controlled;
Python's `zipfile` only detects a declared-vs-actual mismatch via CRC32 *after* a member is fully
decompressed and written to disk. A crafted archive well within the 500MB upload cap
(`controllers/tools.py:330`) with forged small `file_size` fields but highly compressible payloads
can decompress to hundreds of GB before the mismatch is caught. The project's own
`services/import_export/archive_extractor.py:_extract_zip` (lines 299-311) already guards this correctly (caps
each member read, rejects on overrun) — `import_data.py` just doesn't reuse it. A companion gap:
`services/import_formats/gpx.py:41` / `gpx_tracks.py:140` parse GPX XML via `gpxpy` with no
`defusedxml` wrapper (unlike `osm_xml.py`, which correctly uses one) — a latent XXE surface since
`lxml` is installed and `gpxpy` prefers it.

**WebSocket consumers crash on any binary frame, leaking channel-layer group membership.**
Every consumer in `dashboard/consumers.py` (lines 54, 133, 409, 599, 733) declares
`async def receive(self, text_data):` with no `bytes_data` parameter. Channels' base
`AsyncWebsocketConsumer` calls `receive(bytes_data=...)` for any binary WS frame, raising an
uncaught `TypeError` that propagates out of the ASGI coroutine — `disconnect()`/`group_discard()`
never runs, so the dead `channel_name` stays registered in whatever group(s) it had joined. Any
client (buggy library, proxy, or deliberate probe) sending one binary frame on `ws/notifications/`,
`ws/messages/`, or either safety-checkin socket kills the connection ungracefully. Affects all five
consumer classes identically — fix once at the base-class level.

**Two rate-limit/quota checks with the same non-atomic check-then-act race**, each independently
discovered:
- `services/core/rate_limiter.py:341-491` — `check_rate_limit` (COUNT) and `log_api_call` (INSERT) are
  separate operations with no locking; concurrent requests can all pass the check before any log,
  breaching even Nominatim's hard 1 req/sec ToS limit under a handful of simultaneous pin-detail loads.
- `services/security/email_safety.py:106-111` (`email_rate_limit_error`) — same shape for outbound
  friend-invite/visit-invite emails (`controllers/friendship.py:533-570`,
  `services/visits/visit_invites.py:82-109`); concurrent requests can exceed the configured hourly/daily/
  monthly cap arbitrarily.
Both need either `select_for_update()` around the count+log pair or an atomic increment
(`cache.add()`-style) rather than read-then-write.

**AI provider token/cost accounting is silently doubled for 2 of 3 wired providers.**
`services/ai/gateway.py:385-391` calls `self.receive_tokens(message)` in the shared
`send_prompt`/`send_prompt_list`, but `services/ai/anthropic.py:117` and `services/ai/openai.py:121`
*also* call `self.receive_tokens(body)` inside their own `_parse_response()` — double-counting the
same response. `services/ai/cloudflare.py`'s parser correctly does *not* self-call, proving the
other two are a regression rather than by design. Every assistant-chat, document-import, vision-keyword,
and auto-tag cost/token estimate for Anthropic and OpenAI is currently ~2x actual.

**~~Friend-request-visibility bypass via the email-invite path.~~ FIXED - this entry was stale
(verified 2026-07-27).** It described `invite_by_email` checking only
`to_profile.friend_request_visibility != VisibilityChoice.NO_ONE`. That logic has since moved out
of the controller into `services/social/friendship.py:invite_by_email`, which runs the full
`Profile.visibility_permits` evaluator - the same one `request_friend` uses. The non-`NO_ONE`
cases this entry correctly flagged as untested are now covered; see the resolved friend-invite
entry above, including the UX consequence the fix carries.

**`common_pin_count` is shown regardless of its own visibility gate.**
`controllers/userprofile.py:157-158` + `templates/pages/profile/index.html:66-77` — the *count* of
shared pins between two profiles is computed and rendered unconditionally; only the link to the
detail page is gated by `can_view_common_pins_with` (default `FRIENDS`). Any two logged-in
strangers see e.g. "3 Places in Common" even when the setting says only friends should know the
overlap exists at all.

**Login-lockout is keyed on the raw submitted string, not the resolved account — brute-force is
bypassable.** `controllers/account.py:51-58,623-634,657-692` keys failed-attempt/lockout counters
by the literal POSTed "username" value, but `EmailOrUsernameModelBackend`
(`services/auth/auth_backend.py:14-36`) resolves that same field against primary email, verified
secondary email, and Gmail dot/plus-normalized variants before authenticating. An attacker can
brute-force one account indefinitely by rotating through equivalent-but-textually-distinct login
strings (`victim@gmail.com`, `vic.tim@gmail.com`, `v.ictim+x@gmail.com`, a secondary email — each
gets its own untripped counter). `test_email_login.py` proves the login path treats these as
identical; no equivalent test exists for the lockout path.

**`Label` kind-conversion has no branch for converting to Category — silently orphans the row.**
`controllers/labels.py:547-478` (`_apply_kind_conversion`) handles converting to Status/Tag but has
no branch for `new_kind == KIND_CATEGORY`. Converting a global Tag to a Category via the standard
edit form leaves `label.profile=None`, but Category lookups use exact-match `.for_profile()` with
no global fallback — the label vanishes from every Organize > Categories listing, and
`_can_modify_label` returns `False` for any non-tag label with `profile=None`, making the row
**permanently un-editable and un-deletable through the UI** (recoverable only via direct DB access).

**Safety check-in escalation can re-email every emergency contact on any partial failure.**
`services/visits/safety.py:1920-981` (`escalate_checkin`) loops all contacts unconditionally (no
`notified_at__isnull=True` filter) and only saves `status`/`escalated_at` *after* the whole loop
completes. If anything raises mid-loop (bad email address, a non-SMTP/OSError mail-backend
exception), the checkin never flips to `OVERDUE`, so the next 5-minute beat tick re-matches it and
re-emails every contact already notified — including real emergency contacts during an actual
safety incident. Compare `_resolve_as_found_safe` (line 1038-1044), which correctly flips status
*before* its own contact loop for exactly this reason. Compounding gap: the three checkin beat
tasks (`tasks.py:1629-1671`) run every 5 minutes with no locking, unlike the `RUN_LOCK_CACHE_KEY`
pattern already established elsewhere in the same file for this exact problem.

**Undefined CSS custom-property references silently disable dark-mode theming in ~10 SCSS files.**
`var(--text, …)`, `var(--text-muted, …)`, `var(--ul-surface-alt, …)`, `var(--ul-accent, …)`, and
several others are used throughout `_e2ee.scss` (13x), `_setup.scss` (12x), `_markup.scss`,
`_webauthn.scss`, `_messages.scss`, `_gallery.scss`, `_games.scss`, `_trivia.scss`,
`_assistant.scss`, `_pin-detail.scss`, `_profile.scss`, and `_wiki.scss` — but none of these custom
properties is ever defined anywhere (`_tokens.scss`/`_surfaces.scss` grepped, zero matches). Every
one of these rules permanently renders its hex fallback and can never respond to
`[data-theme="dark"]`, despite reading as token-driven. This is a broader, previously-uncaught
instance of the color-token issue the 2026-07-23 `_explainer`/`_map`/`_e2ee` review resolved as
"fine" — that review apparently didn't verify the referenced tokens actually exist.

---

## UL-277: pin-detail external-data freshness window is one global knob, not per-source

**PARKED 2026-07-23 at Jess's request ("skip over this one for right now. I need to reassess
this another day").**

Original wording: "Cache time needs adjustments for some pin details data. Load page, wait 10
minutes, reload page, some items are marked as 'fresh'." The mechanism is technically correct
(`LocationCache.set()` bumps `updated` properly); the actual gap is that `LocationCache.is_stale`
compares against a single site-wide, multi-day `SiteSettings.external_data_cache_days` applied
identically to every external-data source. Implementing this properly means a per-source TTL
override (a field on `PanelSource`/`InfoPanelSource`, or a source→days mapping in
`SiteSettings`) defaulting to the existing global value - plus knowing which sources the
reporter considers too slow to refresh.

---

## Authenticated media gate - residual per-family risk (2026-07-23)

`/media/...` is now served through `dashboard.controllers.media.MediaGateView` (nginx `location
/media/` proxies to Django; authorized responses hand back to the `internal`-only
`/_protected_media/` alias via X-Accel-Redirect). Ownership is enforced per path family where it
is cleanly derivable, but several families intentionally fall back to **authenticated-only**
access (any logged-in user can fetch, no per-object check). Marked with `TODO(media-auth)`
comments in `src/urbanlens/dashboard/controllers/media.py`:

- **`pin_custom_icons/` (Pin.custom_icon) and `label_icons/` (Label.custom_icon)**:
  authenticated-only. Strict owner-only enforcement risks breaking any surface that renders
  another user's shared/labeled pin (shared pin views, trip member maps, global labels with
  `profile=None`). Residual risk is low (small decorative icons, not photos), but a determined
  enumerator could fetch other users' custom icons. Fix would be: owner OR global label OR an
  existing share/visibility relationship.
- **Orphan files** (a file on disk under `pin_images/` or `comment_images/` whose owning
  Image/Comment/TripComment row no longer exists, e.g. row deleted without file cleanup):
  authenticated-only, since there is no owner left to check. Residual risk: pre-existing orphans
  from deletions remain fetchable by any logged-in user who knows the filename.

  **Update (chunk 520, 2026-08-15): the orphan *source* is closed for comments.** Swept every
  delete path: all `Image` paths already removed their file (bulk ones with a shared-file
  reference rule), but `comment.delete()` did not - Django stopped deleting `FileField` files in
  1.3 - so every deleted comment-with-photo stranded a file that this branch then served to any
  authenticated user. Both comment delete paths (pin/wiki and trip) now discard the file;
  `attach_existing_comment_image` copies rather than sharing storage, which is what makes that
  safe. Two tests. The residual risk is now bounded to *historical* orphans and crash windows
  rather than accumulating with normal use - a one-time sweep of `comment_images/` against
  surviving rows would close it entirely.

  **Systematic sweep (chunk 521)**: seven file-bearing model fields exist. `Image.image` and both
  comment images are now handled; explicit *clears* of `Achievement.custom_icon` and
  `Label.custom_icon` now delete their files too (a user pressing "remove icon" is the same
  expectation as deleting a photo). **Still stranding files, recorded not fixed**: replacing an
  icon or avatar with a new upload leaves the previous file, and deleting a Pin/Label/Achievement
  row leaves its icon. Those want a `post_delete`/`pre_save` receiver pair rather than per-caller
  code - the right shape, but a signal touching five models deserves an owner's review rather
  than an audit chunk, and the residual is small decorative icons under an already
  authenticated-only branch.
- **Unknown path families** (anything under MEDIA_ROOT outside the cataloged prefixes
  `pin_images/`, `comment_images/`, `avatars/`, `pin_custom_icons/`, `label_icons/`):
  authenticated-only, logged at INFO. Any future `upload_to` prefix must get an explicit branch
  in `MediaGateView._authorized` or it silently inherits this fallback.
- **`avatars/` (Profile.avatar)**: deliberately any-authenticated-user (avatars render site-wide
  next to usernames) - not a gap, but noted for completeness.
- **Safety check-in photos** (`Image.safety_checkin` set) currently follow the generic
  `Image.objects.visible_to` photo-visibility logic rather than the safety feature's own
  contact-sharing rules; if check-ins are ever shared with emergency contacts who fail the
  photo-visibility check, those contacts would be denied the photos (and vice versa: users
  passing `visible_to` but outside the check-in's audience can fetch them).

**Suggested next step**: product decision on icon visibility (owner-only + share-relationship vs.
authenticated-only), a cleanup job for orphaned media files, and a review of safety check-in photo
audience rules.

---

## Internet Archive: uploader-supplied `subject` tags are a residual noise floor (found 2026-07-22)

The relevance fix matches the location name against `title` OR `subject`. `subject` is
uploader-supplied and unmoderated, so an item tagged with a landmark it isn't actually about still
passes - a live search for `Eastern State Penitentiary` kept `WWE Studio Shots 2006` on a subject
match. Precision is vastly better than before (the same pin previously returned Voice of America
radio broadcasts via full-text matching), and dropping `subject` from `_NAME_FIELDS` would lose
genuine untitled photographs, so this was accepted rather than tightened.

**Suggested next step**: if it proves noisy in practice, rank title matches above subject-only
matches rather than excluding the latter.

---

## Overpass deploy-side follow-up: raise the openresty 90s proxy cap (found 2026-07-22; edge box located 2026-07-23)

The self-hosted Overpass instance (`overpass.osm.urbanlens.org`, now the primary endpoint) sits
behind an openresty reverse proxy that cuts every connection at exactly 90s, regardless of the
Overpass `[timeout:N]` the client requested - the benchmark's only self-hosted failures were
region-scale scans hitting this cap, not Overpass giving up (see
`docs/reports/overpass-mirror-test.md`). Until the proxy timeout is raised above the intended
`[timeout:N]` ceiling, any query needing >90s fails at the proxy.

**Narrowed 2026-07-23**: the Overpass container itself runs on chiron
(`overpass`, `wiktorn/overpass-api:latest`, host port 21890), but the openresty is NOT on
chiron (no 80/443 listener, no openresty/nginx service there; the domain resolves to
163.182.80.211, a separate edge box proxying to chiron:21890). Raising the cap means editing
`proxy_read_timeout`/`proxy_send_timeout` (or the openresty equivalent) on that edge box -
access only Jess has.

---

## Deferred from 2026-07-22: aliases/labels aggregation, and boundary voting

The ROADMAP's "Pin Restructure" section asks for two more things deliberately not attempted as
riders on other work:

**Aliases and labels are not yet aggregated across child pins.** The parent detail page's "show
child pin details" toggle now aggregates map markers, the photo gallery, visit history, and
Notes/comments - but `pin_alias_suggestions` (`controllers/pin.py`) and the
category/tag/status membership panel (`controllers/labels.py`'s `LabelPinMembershipView` /
`label_membership_panel.html`) are both strictly per-pin, with no descendant awareness. Both are
shared generic components also used for Wiki and Image label/alias editing - bolting
hierarchy-aware aggregation onto them risks either duplicating the template or polluting a
generic component with a pin-specific concern. Decide whether aggregation means read-only "also
shown on child pin X" listings (cheapest, matches what comments got) or genuine cross-pin
editing before touching the shared templates.

**Boundary-source voting (REData vs. Overpass, weighted by recency) was not started at all.** It
needs a new model (`BoundaryVote` or similar), a weighting/tie-breaking algorithm, a comparison
dialog with a side-by-side map, and a way to surface "cast a vote" once consensus already exists -
a materially larger, standalone feature (see ROADMAP.md's "Pin Restructure" section, last
bullet, which specifies the weighting rule in detail).

---

## `docker compose exec app pytest` can't reach Valkey in the `s1`/`s2`/`s3` dev environments (found 2026-07-24)

Running the hypothesis suite via `docker compose exec app python -m pytest ...` inside any of
the `~/dev/s1|s2|s3/UrbanLens` environments on chiron fails almost every test that touches a
logged-in request or Celery/Channels broadcast (`realtime.broadcast`, channel-layer setup, etc.)
with:

```
RuntimeError: External network access is disabled during tests. Attempted to connect to
'172.23.0.3'; mock this integration or use localhost.
```

Root cause: `src/urbanlens/core/testing_network.py`'s `LocalhostOnlyNetwork` guard only permits
connections to literal `localhost`/`localhost.localdomain` during tests (by design - see its
docstring). But in these dev environments, `UL_VALKEY_URL` resolves to the `urbanlens_valkey`
docker-compose service, i.e. a docker-network IP (`172.23.0.3` in this instance), not
`localhost` - so anything touching Valkey during a test run trips the guard immediately.

Confirmed this is **environment infrastructure, not application code**: a completely unrelated,
untouched test file (`test_games_controller.py`) fails identically. Meanwhile, pure-DB-layer
tests with no client/channel-layer involvement (e.g. `test_spotguessr_eligibility.py`) pass
cleanly in the same run - so the guard itself and the DB-layer test infra are fine; it's
specifically the Valkey reachability-vs-guard mismatch.

Not investigated further (out of scope for the SpotGuessr UX work this was found during): worth
checking whether `docker-compose.yml`'s `app` service should bind-mount/forward Valkey to
`localhost` for these dev boxes specifically (other deployments may already do this correctly,
or CI may run tests a different way that sidesteps it entirely - e.g. a dedicated test compose
profile). Until fixed, verify backend changes on these dev machines via direct DB-layer/service
tests (no Django test client, no `realtime.broadcast`) plus a manual browser walkthrough against
the running `docker compose up` stack, rather than the full `pytest` suite.

## Setup wizard sidebar reuses inverting `--ul-grey-N` tokens on an always-dark panel (found 2026-07-25)

`_setup.scss`'s `.setup-wizard__sidebar` sets `background: rgba(0,0,0,0.3)` on top of whatever
the parent `@include surface()` background is - like the map filter panel and a few other
"always dark" chrome panels called out in `_tokens.scss`'s comments, it reads as a fixed dark
strip in both themes rather than genuinely inverting. But unlike those other always-dark panels,
its child text (`.brand-name`, `.setup-stepper__item`, etc.) uses the regular inverting
`var(--ul-grey-1)` / `var(--ul-grey-5)` / `var(--ul-grey-6)` / `var(--ul-grey-7)` tokens. In light
mode `--ul-grey-1` is a near-white (`#dfdfdf`), which reads fine against the dark sidebar overlay.
In dark mode `--ul-grey-1` flips to a near-black grey (`$color-grey-8`, `#373737`), which is
low-to-no contrast against that same dark sidebar background - the sidebar text likely becomes
close to unreadable in dark mode. Not fixed here because it's a structural mismatch (the
component assumes a static-dark treatment but was styled with tokens meant to invert), not a
missing/undefined custom property - fixing it means either converting the sidebar's own text
tokens to a fixed light-on-dark scheme (like `_tokens.scss`'s `$ui-fp-*`/`$ui-link-on-dark`
pattern for the map filter panel) or making the sidebar itself genuinely theme-aware. Worth a
manual dark-mode check of `/setup` before shipping.

## Safety check-in partners: two residual gaps found during a fresh-eyes feature review (2026-07-25)

A full review of the partner/live-location/post-resolution-encryption feature (two independent
review agents, backend-correctness and frontend-security) found and fixed nine issues directly
in `services/visits/safety.py`/`consumers.py`/`tasks.py`/`models/safety/model.py` (archival payload not
capturing/severing `destination_location`/`trip`/`markup_map`/`markup_maps`, `archive_checkin`
non-atomicity, chat messages postable after archival, three TOCTOU races, a missing index, an
N+1, and no live-connection revocation on partner removal - all covered by new tests in
`test_safety_archival.py`/`test_safety_partners.py`/`test_safety_live_location.py`/`test_safety.py`).
Two narrower items were identified but deliberately left open:

- **Blocking a partner doesn't revoke their existing access.** ~~`Profile.are_blocked` creation has
  no signal wired to `SafetyCheckinPartner` cleanup~~ **RESOLVED 2026-08-17 (chunk 580).**
  `block_profile` now calls `remove_checkin_partner` for every `SafetyCheckinPartner` row between
  the two profiles in either direction, which was this entry's own prescription - that helper
  already deletes the row *and* force-closes any live WebSocket, so nothing new was needed beyond
  calling it. Outstanding invitations are revoked alongside accepted rows, an unaccepted invite
  being an offer of exactly the access in question. Covered by
  `test_block_revokes_safety_partner.py`, including that an unrelated partner on the same check-in
  is untouched. The deferral here was review scope, not principle.
- **A malformed/corrupted `MessagingKeyBundle.public_key` makes `archive_checkin` fail forever,
  loudly but without escalation.** `archive_checkin` now isolates one checkin's failure from
  others in the 5-minute sweep (a bad row no longer blocks the rest of the batch) and every
  attempt is logged via `logger.exception`, but there's still no cap or alerting on repeated
  failures for the *same* checkin - it will silently retry and re-fail every 5 minutes
  indefinitely if a specific owner's key bundle is genuinely corrupt, with only a log line as the
  trail. Not expected to happen in practice (enrollment writes a fresh valid key), but there's no
  guard against it happening anyway (e.g. a future data-migration bug). Worth a "give up and flag
  for manual review after N failed attempts" backstop if this class of bug ever surfaces in
  practice.

## Full-codebase audit: re-verification pass (2026-07-25)

After the initial 35-unit audit (above) was worked through fix-by-fix in an earlier session, six
independent re-verification passes re-read every finding in `docs/notes/ai/codebase-audit.md`
against the current code (not trusting the earlier session's own claims) and reported per-finding
FIXED/PARTIALLY-FIXED/NOT-FIXED/REGRESSED verdicts. Most findings held up as genuinely fixed; the
handful of regressions and higher-value gaps the re-verification surfaced were fixed directly in
this pass:

- **`services/ai/openai.py`'s `get_client()`** unconditionally passed `base_url=str(self.api_url)`
  to the OpenAI SDK. `OpenAIGateway.setup()` never actually sets `api_url` (unlike the
  Cloudflare/HuggingFace gateways), so this was always `str(None)` - the literal string `"None"` -
  meaning a real OpenAI call would have tried to connect to that instead of the SDK's real default
  endpoint. Pre-existing bug, not a regression from the earlier fix pass; now only passes
  `base_url` when set.
- **SpotGuessr's new reverse-geocode cache (`services/spotguessr/geo_bonus.py`) treated a rate-limit
  failure as a genuine "no result" and cached it for the full 30-day TTL** - a transient Nominatim
  rate-limit hit (the exact failure mode the cache exists to work around) would have silently
  disabled the country/state/city bonus for an entire ~111m cell for a month. `reverse_geocode_admin`
  (`services/apis/locations/nominatim.py`) now lets request/transport failures propagate instead of
  swallowing them to `None`, and `geo_bonus.py` gives a failed lookup a 60-second TTL instead of 30
  days, while a genuine "nothing found" result still gets the long TTL.
- **The undo framework's `stash_for_undo()` calls in `pin_bulk.py`, `detail_pins.py` (×2), and
  `location_wiki.py` ran *before* the `transaction.atomic()` block wrapping the delete**, with a
  comment claiming the atomic wrapper prevented a partial-delete-with-stashed-undo inconsistency -
  it didn't, since the stash (an immediate `UndoAction.objects.create()`) had already committed
  before the atomic block even opened. Moved the stash call inside each atomic block, before the
  delete, so a mid-delete failure now rolls back both together.
- **The storage-quota check-then-create race (`services/media/storage.py`'s `per_profile_upload_lock`)
  was only wired up at 2 of 8 call sites** (`photos.py`, `image_gallery.py`) - `article.py`,
  `direct_messages.py`, `maps.py`, `safety.py`, `tools.py`, and `visits.py` still raced. All six now
  wrap their check-then-create in `per_profile_upload_lock`.
- **`LocationManager.get_nearby_or_create()`** (unlike `PinManager`'s already-fixed version) had no
  `try/except IntegrityError` around its `create()` call, despite `Location` having a real
  `unique_together = ["latitude", "longitude"]` constraint that two concurrent requests creating a
  Location at the exact same coordinates could hit. Now catches it and returns the
  concurrently-created row, matching `PinManager`'s pattern.
- **`services/messaging/direct_messages.py`'s email/text-alert debounce (`is_email_debounced`/
  `is_text_alert_debounced`) was still a plain `cache.get()` check-then-later-`cache.set()`** - the
  same TOCTOU shape already fixed in the sibling `notification_text_alerts.py` via atomic
  `cache.add()`. Ported the same fix; the now-redundant `cache.set()` calls inside
  `send_message_email_now`/`send_message_text_alerts_now` were removed since the marker is claimed
  atomically by the check itself. (`test_direct_messages.py`'s debounce test was rewritten to
  exercise this through the real task entry point, matching how the sibling module's tests already
  verify the same pattern.)
- **`services/ai/assistant.py`'s `_tool_add_trip_activity`** had the identical TOCTOU race
  (`trip.activities.count() >= max_activities` check-then-create) that `_tool_create_trip` and
  `link_extraction.start_link_extraction` had just been fixed for in the same pass - it wasn't
  itself covered. Now locks the `Trip` row (not the profile - the count is per-trip, and other
  members can add activities to the same trip) for the check-then-create.
- **Duplicate, conflicting dark-mode CSS for `.subscription-admin-page .role-pill`** - independent
  fix passes had added a `[data-theme="dark"]` override in both `_admin.scss` and `_dark.scss`,
  with different colors; `_admin.scss`'s `@use` order meant its version always won, making the
  `_dark.scss` copy dead and misleading. Removed the dead copy.
- **The undo framework's `MODEL_LABEL` constants** (added to `handlers/pin.py`, `handlers/wiki.py`,
  `handlers/safety_checkin.py` specifically to stop call sites hand-typing `"pin"`/`"wiki"`/
  `"safety_checkin"` as bare strings) were never actually imported at any of the ~8
  `stash_for_undo(...)` call sites - the fix added the constants but didn't wire them up. Added the
  missing `MODEL_LABEL` constants to `handlers/saved_filter.py`/`handlers/trip.py` and updated every
  call site (`pin_bulk.py`, `detail_pins.py`, `location_wiki.py`, `safety.py`, `saved_filters.py`,
  `trip.py`, `models/pin/viewset.py`) to import and use the shared constant instead of a literal.

**Confirmed still open** (verified genuinely unfixed, not worth blocking on for this pass - listed
here so the next session doesn't have to re-derive them from `docs/notes/ai/codebase-audit.md`'s
full per-unit detail):

- `services/messaging/direct_messages.py`'s TOCTOU fix above only covers the DM email/text debounce; the
  underlying **`quota_error_for_upload`/`per_profile_upload_lock` pattern itself is a "soft" lock**
  (proceeds without the lock if it can't be acquired promptly) - fine for its stated purpose but
  worth remembering it's not a hard guarantee.
- **Unit 08**: `pin.py`'s `media_send_to_wiki` still synchronously downloads up to 20 media items
  in the request handler; no shared upload helper exists despite the sequence being duplicated
  across ~8 call sites now sharing the same lock.
- **Unit 09/10**: bulk-accept/reject's per-item failures still aren't surfaced in the frontend
  toast; both trip-invite paths and calendar-push still loop per-invitee/per-activity without
  batching or debounce; `TripActivity.order` still has no uniqueness constraint or locking.
- **Unit 13/14/19**: `controllers/labels.py`'s `ai_kind_enabled`/`keyword_kind_enabled` duplication
  between `.get`/`.post` is unchanged; `NotificationPreference` still only models 12 of 30
  `NotificationType` values; no admin can see/revoke another admin's subscription grants; no
  restore tooling exists for the Postgres backups.
- **Unit 20**: `PinSerializer.create()` and `parse_for_preview` still make synchronous/blocking AI
  calls in the request cycle rather than via Celery; `services/ai/huggingface.py` is still an
  unwired, `NotImplementedError`-raising stub (now explicitly documented as such, rather than a
  silent dead end).
- **Unit 21/22/23**: `models/pin/viewset.py`'s post-`get_object()` ownership re-check is still dead
  code (queryset already filters it); `GroupMessage` still carries no images/markup_map/
  location_mentions/reply_to fields; `GameSessionConsumer`/`TriviaSessionConsumer` are still
  near-duplicate classes with no shared base, no per-connection rate limiting on any WS `receive()`.
- **Unit 24/25**: the SpotGuessr/Trivia `eligible_locations()`/`eligible_questions()` retry loops
  still re-run the full query on every attempt instead of computing the eligible set once; no
  moderation UI exists for AI-flagged trivia questions (decided against, not just unbuilt - see
  `docs/designs/drafts/trivia.md`'s "Known gaps"); Trivia gained a leave/kick path plus
  stall-handling parity with SpotGuessr on 2026-07-25 (`services.trivia.session.leave_session`/
  `kick_participant`/`force_reveal_round`/`end_session_now`) - SpotGuessr itself still has no
  leave/cancel/kick path once a lobby exists.
- **Unit 31**: `_dark.scss` is still ~1100 lines of per-selector overrides (the role-pill fix above
  removed one duplicate, not the pattern); `_pin_lists.scss` still has 3 sibling raw-hex danger-red
  controls without dark overrides (`.pin-list-more-menu-danger`, `.saved-filter-delete-btn`, and its
  hover state) that PROBLEMS.md already flagged as a follow-up.
- **Unit 34**: only ~30/111 `@given`-using test files import the shared `strategies.py` module
  (up from 8/97, but still a minority); `test_trivia_wiki_incorporation.py` has zero `@given` tests
  despite an obvious property-testing candidate (the upvote-count threshold logic); a prior
  session's claim that hypothesis tests were added to `test_safety.py`/
  `test_safety_checkin_slugs.py`/`test_trip_controller.py` does not hold up under inspection - those
  three files still have zero `@given` tests (only `test_trip_helpers.py` and the two genuinely new
  files, `test_safety_archival.py` and `test_trivia_wiki_incorporation.py`, show real hypothesis
  work, and the latter's own tests are all hardcoded-value examples).

All of the above are maintainability/completeness gaps, not active security or correctness bugs
(those categories were the ones fixed directly, above) - reasonable to pick up as a dedicated
follow-up rather than blocking this pass.

## `docker compose up`'s app container fails `manage.py migrate`'s implicit check with a PinViewSet `AssertionError` on `s2` (found 2026-07-25, not root-caused)

While standing up the Consensus game feature (new models/services/consumer/URLs), tried to verify
against a live stack on the `s2` dev environment (`~/dev/s2/UrbanLens` on chiron). The full
`docker compose up -d --build` failed - the `app` container's entrypoint init script runs
`manage.py migrate`, which runs Django's implicit system checks first, and that failed with:

```
File ".../dashboard/urls.py", line 93, in <module>
    router.register("pins", PinViewSet, basename=PinViewSet.basename)
File ".../rest_framework/routers.py", line 170, in get_default_basename
    assert queryset is not None, '`basename` argument not specified, and could ...'
AssertionError: `basename` argument not specified, and could not automatically determine the name...
```

`PinViewSet.basename = "pins"` is a plain class attribute and `dashboard/urls.py:93` passes it
explicitly (`basename=PinViewSet.basename`) - by inspection this should never reach
`get_default_basename` at all, since DRF's `SimpleRouter.register()` only calls that when
`basename` is `None`. Confirmed installed `djangorestframework==3.17.1` is identical on both `s2`'s
container and the local Windows venv, where the equivalent `manage.py check` (run directly, not via
`migrate`) passes cleanly every time. Root cause not found - ran out of scope budget chasing it
while `s2` was also independently blocked on an unrelated stale-migration-graph issue (below), which
was the one actually relevant to this session's work.

Worked around entirely by bypassing the custom entrypoint (`docker compose run --rm --entrypoint ''
app .venv/bin/python -m pytest ...`), which never imports the full URLconf (pytest doesn't eagerly
resolve URLs unless a test actually calls `reverse()`/hits a view) - this is how the Consensus
DB-backed test suite ended up getting verified despite this. Not confirmed whether this also affects
a genuinely fresh checkout with no other changes (an `s3` attempt at the same thing got stuck at
container state `Created` with zero log output before this could be isolated) - worth a fresh,
focused repro next time someone needs `docker compose up`'s full stack (not just `docker compose
run`) on one of these dev boxes.

## Dev environments (`s1`/`s2`/`s3` on chiron) can silently drift behind `origin` (found 2026-07-25)

`~/dev/s2/UrbanLens` was one full commit behind `origin/@release/v0.6.0` (missing
`0017_spotguessr_participant_rating_delta.py` and everything else in the "Implement multiplayer
enhancements... for SpotGuessr" commit) despite `git status` reporting clean - a `git fetch`/`log`
comparison is needed to actually notice this, since "clean working tree" says nothing about how
current the checked-out commit is. This produced a confusing
`NodeNotFoundError: ... dependencies reference nonexistent parent node ('dashboard',
'0017_spotguessr_participant_rating_delta')` when testing a new migration that (correctly, per this
same file's `makemigrations`-dependency gotcha) depended on the latest *committed* migration - the
dependency was fine, the dev box just didn't have it yet. Fixed for this session by `git fetch` +
`git pull --ff-only` (stashing/resolving trivial conflicts in files also touched by the missed
commit, e.g. `CELERY_BEAT_SCHEDULE` dict entries landing near each other) - worth checking `git log
origin/<branch> -1` vs. local `HEAD` up front, not just `git status`, when picking a "free" dev
environment for migration-touching work.

## Residues left by the TEMPORARY legacy-CID coordinate repair (found 2026-07-25)

`services/apis/locations/legacy_cid_coordinate_fix.py` lets a re-import move a user's
pre-2026-07-25 pins off the coordinates the old S2-decode guess put them on. Two known gaps
that it deliberately does *not* close - both should disappear when that module is deleted,
but re-check them then rather than assuming:

1. **The CID stays on the bad `Location`.** `GooglePlace.cid` is `unique=True`, so the repaired
   pin's new (correct) Location can't claim the CID while the old, wrongly-placed Location still
   holds it - the backfill in `_create_pin_from_confirmed` is skipped for exactly this case.
   Consequence: `Location.objects.by_cid()` keeps resolving that CID to the wrong Location for
   *every* user, and each re-import pays a fresh REData/Places resolution instead of a cache hit.
   Repointing the CID would fix it globally, but it mutates shared cross-user data off the back of
   one user's import, which is why it wasn't done here. Deliberate call, not an oversight.

2. **`GoogleMapsGateway.import_pins_streaming` was left un-repaired.** It's the older one-shot
   `pin.upload.takeout` path, and it still places CID pins from `extract_coordinates_from_url`'s
   S2 decode - i.e. it can still create wrongly-placed pins today. Nothing in
   `templates/dashboard/pages/location/import/csv.html` (or anywhere else) references that URL;
   the UI goes through `pin.import.preview` -> `pin.import.confirmed`, which defers CID pins
   properly. The repair was not wired into it because doing so safely means giving it the same
   deferral machinery, not because it's correct as-is. Either give it the deferral path or delete
   the route and `import_pins_streaming` with it - a live URL that silently mis-places pins is
   worse than no URL.

## Messaging / external API (noted 2026-07-26, during the mobile v2 messaging API build)

- **WebSocket credential auth does no per-scope check.** `ApiKeyAuthMiddleware` (see
  `src/urbanlens/dashboard/websocket_auth.py` and its use in `UrbanLens/asgi.py`) authenticates
  a WebSocket connection from any valid, unrevoked credential and then grants blanket access -
  it never consults the connection's scopes. So a token issued with, say, only `pins:read`
  can still open `ws/messages/` and `ws/notifications/` and receive live direct-message and
  notification payloads, which is precisely the data `OAUTH2_ONLY_SCOPES` restricts to
  user-consented OAuth2 tokens on the HTTP side. The HTTP messaging endpoints added in
  `external_api/views_messaging.py` enforce `messages:read`/`messages:write` correctly; the
  socket path is the remaining hole, and it currently undercuts that enforcement because the
  same data is reachable over the socket without the scope.
  **Deliberately not fixed in that pass** (scope control - it touches three consumers and
  their tests, and a mistake there disconnects working *web* clients, whose session auth flows
  through the same middleware). **Fix shape**: give each consumer a required-scope declaration
  and have the middleware/consumer `close()` with a 4403-style code when a credential-authed
  connection lacks it, leaving session-authed connections (`request.auth is None`) untouched -
  the same session-or-credential split `external_api/mixins.py:IsSessionAuthenticated` already
  draws for HTTP. Needs tests for: scoped token accepted, under-scoped token rejected, session
  unaffected, PAT rejected outright on messaging sockets.

- **Markup-map attachments bypass share provenance.** Attaching a `MarkupMap` to a direct
  message (`create_direct_message(markup_map_uuid=...)`, and the `send_message_with_share`
  path in `services/messaging/direct_message_shares.py` when no `shared_pin_id` accompanies it) records
  **no `LocationExposure`**, even though a markup map can depict pin locations and therefore
  can disclose them to the recipient. Sharing the *pin* correctly stamps the chain via
  `create_pin_share` -> `resolve_and_stamp_origin_share` + `record_share_exposure`; attaching a
  map that draws the same place does not, so the location's re-share history silently has a
  hole in it. **Not a regression** - the web composer has always behaved this way and the new
  API endpoint merely matches it, which is why it was documented rather than changed
  mid-build. **Fix shape**: on attach, resolve the `MarkupMap`'s items to the pins/locations
  they reference and record an exposure per distinct location, reusing `record_share_exposure`
  rather than inventing a second provenance path. Decide first whether a hand-drawn annotation
  with no linked pin should count (probably yes if it carries coordinates).

- **Three pre-existing mypy errors surface whenever anything type-checks the external API's view
  module** (found 2026-07-26 while adding the lists/labels external endpoints; none are caused by
  that work, and all three live in files it does not touch):
  - `dashboard/models/boundary/queryset.py:87` - `"GEOSGeometry" has no attribute "exterior_ring"`
    in `buffer_point_by_meters`. `Point.buffer()` is typed as returning the `GEOSGeometry` base
    class, but the code relies on the result actually being a `Polygon`. Wants a narrowing
    `assert isinstance(circle, Polygon)` (or a typed helper) rather than a `cast` - the runtime
    assumption is genuinely unchecked today.
  - `dashboard/forms/search.py:176` - `resolve_reference(...)` is handed `self.profile`, typed
    `Profile | None`, where a non-optional `Profile` is expected. Either `SearchForm.profile`
    should be non-optional or this branch needs an explicit None guard; as written, a form built
    without a profile would fail here at runtime.
  - `dashboard/controllers/trip.py:481` - `existing_ids.add(trip.creator_id)` where `creator_id`
    is `int | None`, so a trip with no creator would insert `None` into a `set[int]`.

  All three look like real latent bugs rather than annotation noise, which is why they are logged
  here instead of silenced. They don't fail today because mypy isn't run across these paths
  together - they only appear once `external_api/views.py` pulls them into one type-check graph.

- **Pin-detail's `wiki_slug` was unusable for navigating to a wiki (FIXED in this pass).**
  `services/pins/pin_detail.py::build_pin_detail` set `payload["wiki_slug"] = wiki.slug`, which reads
  naturally as "the slug to fetch this pin's wiki with". It isn't. Every wiki-scoped route
  resolves through `services.wiki.wiki_access.resolve_visible_wiki`, which takes a **Location**
  slug/uuid - and `Wiki.slug` is an independent `SlugField` on an unrelated model with its own
  value. A client that followed `wiki_slug` to `GET /wikis/{location_slug}/` therefore got a 404
  for a wiki it could plainly see. Fixed by adding `location_slug` (from
  `location.ensure_slug()`) to the payload and to `PinDetailSerializer`; `wiki_slug` is retained
  but documented as informational-only. Regression test:
  `tests/hypothesis/test_external_api_pin_detail_location_slug.py`.

- **The internal wiki edit view silently discards invalid input (NOT fixed - deliberate).**
  `controllers/location_wiki.py::LocationWikiEditView.post` iterates the editable fields and
  `continue`s past (a) a security value not in `SecurityLevel.choices` and (b) a date that fails
  `datetime.strptime(raw, "%Y-%m-%d")`. The user is told `{"ok": True}` and the field simply
  never changes, with no error surfaced anywhere - a submitted-but-dropped edit is
  indistinguishable from a successful one. The shared `services/wiki/wiki_edits.py::apply_wiki_edit`
  extracted in this pass takes a `strict` flag: the external API passes `strict=True` and gets a
  hard rejection, while the internal path keeps `strict=False` to preserve existing HTMX
  behavior. The internal path should be migrated to strict (with proper field-level error
  rendering in the About card) as a follow-up - it needs UI work, which is why it was left alone
  here rather than changed blind.

- **A wiki's "First pinned" date leaked past the low-pin-count privacy fuzz (FIXED).**
  `approximate_pin_count` deliberately refuses to show a number until at least
  `MIN_VISIBLE_PIN_COUNT` (3) distinct users have pinned a place, but the Community card showed
  "First pinned <Mon YYYY>" *unconditionally*. With only one or two pinners, that month is
  effectively "when this specific person pinned it" - exactly what the count fuzzing exists to
  hide. (The template already rendered `|date:"M Y"`, so the day was never displayed; the leak
  was the missing low-count suppression, and the fact that day-precision sat in the template
  context at all.) Fixed by `services/wiki/community_counts.py::wiki_community_summary`, which
  truncates `first_pinned` to the 1st of its month and returns `None` whenever `pin_count_low`
  is true. Both `LocationWikiView` and the external API now read that one function, and
  `wiki.html` renders the pre-truncated date rather than reaching into a Pin instance.

- **`MapController.resolve_place` does not honor the `external_apis_enabled` profile toggle**
  (`src/urbanlens/dashboard/controllers/maps.py:384-408`). Its sibling
  `autocomplete_places` (same file, line ~361) *does* check
  `request.user.profile.external_apis_enabled` and returns `{"disabled": true}` when the user
  has turned external lookups off - but `resolve_place`, which is called the moment the user
  *selects* one of those suggestions, checks only whether an API key/REData is configured. So a
  user who has opted out of external API calls still triggers a Google Places **Details** call
  (billable, and a privacy leak of what they searched for) on selection. Repro: set
  `Profile.external_apis_enabled = False`, GET
  `/dashboard/map/resolve-place/?place_id=<any>` -> still hits the provider. Fix: add the same
  `if not request.user.profile.external_apis_enabled: return ... 403` guard the external API's
  `PlaceResolveView` now applies
  (`src/urbanlens/dashboard/external_api/views.py`, `PlaceResolveView.get`). Noted while
  building the external `locations/resolve/` endpoint, which deliberately does *not* reproduce
  the omission; the internal path was left alone to keep that change out of an already large
  API-surface commit.

- **`test_spotguessr_geo_bonus.BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising`
  fails on a polluted cache, not on the code under test**
  (`src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py:102-108`). The test
  patches `NominatimGateway` to return `None` and expects a 0-point bonus, but gets 750 (all
  three tiers). `services/spotguessr/geo_bonus.py::_reverse_geocode_admin_cached` memoizes the
  reverse-geocode result in the Django cache keyed by *rounded* coordinates, and the earlier
  tests in the same class populate that key with a matching admin dict - so the patched gateway
  is never called and the failure branch is never exercised. Reproduces with the file run
  alone (`pytest src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py`), so it
  is not a cross-file interaction. Pre-existing: neither `geo_bonus.py` nor its test has been
  touched. Fix is a `cache.clear()` in that class's `setUp` (and arguably a project-wide
  `LocMemCache` reset between tests, since any cache-backed service has this hazard). Noted
  while building the external SpotGuessr API; left alone because the file belongs to another
  work stream.

- **The inbox list serializes each conversation's last message with no reaction/share
  prefetch** (`src/urbanlens/dashboard/services/messaging/direct_messages.py::conversations_for`,
  `src/urbanlens/dashboard/services/messaging/group_chats.py::group_conversations_for`). Each row's
  `last_message` is rendered through `build_direct_message_payload` /
  `build_group_message_payload`, which read `message.reactions.all()` (and, for groups,
  `message.share_for(viewer)`), so a page of N conversations issues ~2N extra queries. Both
  builders are correct; the missing piece is a prefetch on the *selected* last messages.
  It cannot simply be added to the existing queries: `group_conversations_for` scans every
  visible message across every group in order to pick the newest per group, so prefetching
  there would pull reactions for the whole history rather than for the N rows actually
  rendered. The fix is a second, id-bounded `prefetch_related_objects()` call over the
  already-selected `last_message` instances. Pre-existing on the 1:1 side; noted while adding
  reactions/`pin_share_id` to the group payload (`external_api/serializers_messaging.py`),
  which gave the group side the same shape. The thread endpoints - where a page is 50 messages
  rather than 1 - already prefetch, so this is an inbox-only cost.

- **`test_avatar_colors.GroupMemberSearchAvatarColorTests::test_results_get_distinct_colors`
  returns 0 results where it expects 4**
  (`src/urbanlens/dashboard/tests/hypothesis/test_avatar_colors.py:105-111`). The test creates
  four ANYONE-visible profiles named `searchable-user-<n>` and expects
  `GET messages.group.member_search?q=searchable-user` to return all four;
  `response.context["results"]` is empty. The candidate filter in
  `controllers/group_chats.GroupMemberSearchView` (and the `can_direct_message` gate it leans
  on in `services/messaging/direct_messages.py`) is the place to look - the test sets
  `user.username` directly with `save(update_fields=["username"])`, so a search that reads a
  denormalized/`Profile`-side name would match nothing. Both of those modules carry
  uncommitted edits from another work stream, and nothing in the social/avatar/annotation
  change this was found under touches conversation membership or direct-message gating.
  Noted while running `test_avatar_colors.py` as a regression check for the avatar-write
  extraction (`services/profile/avatar.py::set_profile_avatar`); left alone as it belongs to the
  messaging work stream.

- **Blocked `Friendship` rows created before `block_profile` started normalizing direction may
  record the wrong blocker** (`src/urbanlens/dashboard/services/social/friendship.py::block_profile`).
  `Friendship` has no "blocked_by" column, so `from_profile` is the only record of who blocked
  whom, and `block_profile` used to reuse whichever row already joined the pair - a block
  placed on an inbound friend request therefore left the *blocked* party as `from_profile`.
  It now re-points the row so `from_profile` is always the blocker, which fixes every block
  placed from here on, but existing rows carry no signal that could be used to repair them:
  a data migration would have to guess. Impact on a legacy row is bounded and inverted from
  the original P0 - the true blocker gets a 404 from `unblock_profile`/`remove_friend` and
  must re-block to normalize the row, and the blocked party can lift it. Worth a one-off
  audit query (`Friendship.objects.filter(status="Blocked", created__lt=<deploy>)`) rather
  than an automated migration.

## 2026-07-28: `services/consensus/fields.py` - 9 pre-existing `[has-type]` mypy errors

Found while running a full `mypy src/urbanlens/dashboard` sweep as part of the external-API P2
parity-polish pass's Phase 8 prep (Games polish - SpotGuessr/Trivia/Consensus). Not caused by this
pass - nothing in this session touches `services/consensus/fields.py`, and `git log` shows it
predates this branch's work (Consensus was built 2026-07-25, per a separate session).

All nine errors are `Cannot determine type of "<field>"  [has-type]` on lines 315/316 (`name`),
322/323 (`description`), 329/330 (`indoor_outdoor`), 338/339/339 (`pin_type`,
`pin_type_is_user_provided`) - each inside a lambda (`current_value=lambda w: w.name`, etc.) passed
as a keyword argument to `_wiki_field_strategy(...)` while building the `_STRATEGIES` dict. `w` is a
`Wiki` instance and every one of these is an ordinary model field, so this isn't an obviously wrong
runtime assumption the way the boundary/queryset.py and forms/search.py entries above are - it looks
like a mypy inference limitation on the lambda's implicit parameter type when `_wiki_field_strategy`
itself is generic/`Callable`-typed, rather than a real bug. Left uninvestigated because Phase 8 does
not touch `_STRATEGIES` or the field-strategy machinery, only Consensus's session/eligibility/vote
services - fixing this would mean guessing at `_wiki_field_strategy`'s intended generic signature
without the context of whoever wrote it.

## 2026-07-30: `test_media_auth_mixin.py::MediaAuthResolutionTests::test_session_wins_over_a_credential_header` is flaky (PK off-by-one)

Found while running the media/search/public-pins suites for an unrelated PR #126 review-comment pass
(scoping fixes in `external_api/views_search.py`, `controllers/media.py`, `services/pins/public_pins.py`
- this test file was never touched). Fails both in isolation and alongside other files, non-
deterministically off by exactly one: `AssertionError: '17' != '16'` in one run, `'194' != '193'` in
another. The assertion is `self.assertEqual(response.content.decode(), str(self.profile.pk))` -
comparing the profile pk baked in `setUp` against whatever pk the view actually resolved, so either
an extra `Profile`/`User` row is being created somewhere between `setUp` and the assertion (shifting
the auto-increment sequence out from under the hardcoded expectation), or the mixin under test is
genuinely resolving the wrong profile. Needs a session review of `MediaAuthResolutionTests.setUp`
and `resolve_media_profile`/`CredentialOrSessionMediaMixin` to tell which; not investigated further
since it's unrelated to the search/media/public-pins scoping fixes this session was making.

## 2026-07-30: Two Google API keys are HTTP-referrer-restricted but only ever called server-side - every request gets a 403

Found from production logs: `google_images.py`'s `GoogleImageSearchGateway` (`customsearch.googleapis.com`)
and REData's `redata_places_gateway.py` (`places.googleapis.com`) both started failing with
`403 Requests from referer <empty> are blocked. (forbidden)`. Neither gateway sends or fakes a
`Referer` header - `google.py`/`google_images.py` just does a plain `self.session.get(...)`, and
REData's gateway passes its key via the `X-Goog-Api-Key` header (see
`../REData/src/redata/parcels/services/google_places_details/gateway.py:189-193`) - both textbook-
correct for a server-side key. The 403 is Google's API itself rejecting the request, because
whichever key backs `settings.google_domain_restricted_api_key` (UrbanLens) and `RD_GOOGLE_MAPS_API_KEY`
(REData) is configured in Google Cloud Console with an **HTTP referrers (websites)** application
restriction. That restriction type only works for browser-side calls (Maps JS API, embedded widgets)
where a real `Referer` header is present - a Celery worker calling Google's REST API directly never
sends one, so Google always sees `<empty>` and blocks unconditionally, independent of the key being
otherwise valid/enabled/correctly configured in env vars.

Not fixable in code on either side - short of literally fabricating a `Referer` header to spoof a
browser origin, which would be actively wrong to do. The real fix is a Google Cloud Console change:
open each key's Credentials page and change "Application restrictions" from "HTTP referrers" to
"IP addresses" (the production egress IP(s)) or "None", leaving "API restrictions" (which Google
APIs the key may call) untouched. Requires Cloud Console access neither this session nor the
REData session had. Confirm both keys - UrbanLens's Custom Search/Image Search key and REData's
Places API (New) key may or may not be the same underlying Google Cloud project/key.

## 2026-07-31: REData's `/api/v1/parcels/lookup/` is in an OOM/WORKER-TIMEOUT crash loop on chiron

Found while investigating the `resolve_deferred_pin_locations` retry-forever bug below - unrelated
endpoint, noticed in the same gunicorn log sweep on `redata-production-app-1`. Repeated `WORKER
TIMEOUT` followed by `SIGKILL` and worker respawn, i.e. requests to that endpoint are exhausting
memory or wall-clock badly enough for gunicorn's own supervisor to kill the worker. Not
investigated further - REData is a separate codebase/service another agent maintains (per
`CLAUDE.local.md`), and this session only had read access there. Whether this crash loop
contributed to or is independent of the CID-resolution backlog (both endpoints share the same
gunicorn workers, so one starving the other for memory is plausible) was not determined.

## 2026-07-31: Production celery worker's `.env` has `UL_REDATA_API_URL`... but check `UL_SITE_URL=staging.urbanlens.org`

Noticed while inspecting `redata-production-app-1`'s environment (via scoped, non-secret-exposing
`grep` - see below) during the CID-resolution investigation: a variable read off what's supposed to
be the *production* UrbanLens celery worker's environment showed `UL_SITE_URL=staging.urbanlens.org`.
That looks like a copy-paste/deploy-config leftover from a staging `.env`, which would make any
absolute URL the production worker builds (e.g. notification deep-links via `request.build_absolute_uri`
equivalents, `reverse()`-based URLs sent in emails/notifications) point at staging instead of
production. Not confirmed as a real production `.env` (vs. this session misidentifying which
container/host it was inspecting) and not fixed - purely operational (an env var value on the
deployed host, not a code change) and outside this session's remit. Worth a human checking the
actual production `.env` deploy config directly.

## 2026-07-31: `resolve_deferred_pin_locations` retried every 120s forever against a REData cid stuck behind its own 30-day cache floor - fixed

Root cause of a production incident: importing `sample_data/Google Takeout.csv` deferred ~700 cids
to REData's `POST /places/resolve-cids/` for resolution. REData's `StaggeredCachePolicy`
(`core.services.staggered_cache.py`, `min_ttl_hours=720` i.e. 30 days by default) has a hard floor -
`should_refresh()` returns `False` unconditionally for any row younger than `min_ttl_hours`,
regardless of quota utilization. `needs_refresh(place)` is just `should_refresh(place.last_checked_at)`,
so the instant a `GooglePlace` row gets checked even once (`last_checked_at` stamped) without reaching
the 3-attempt `confirmed_no_location` terminal state, REData will not queue another resolution attempt
for it for weeks - but keeps reporting it as `pending` (HTTP 200, no error) on every subsequent
`resolve-cids` call, since it's neither `resolved` nor `confirmed_no_location`. Confirmed via direct
DB query on chiron: 441 of 723 `GooglePlace` rows stuck `resolved=False, confirmed_no_location=False`
with zero `last_checked_at` activity for 10+ hours.

UrbanLens's `resolve_deferred_pin_locations` (`dashboard/tasks.py`) treated "still pending, REData
responded fine" as forward progress and retried every 120s with `max_retries=None` and no ceiling -
the existing `consecutive_request_failures` cap (added in an earlier pass, commit `e7a10584`) only
covers whole-batch *request* failures, not "REData responded successfully but nothing moved." Fixed
by adding a second, independent `consecutive_no_progress` counter/cap (`_MAX_CONSECUTIVE_NO_PROGRESS_RETRIES`)
that only increments when a retry's `result.pending` is the exact same size as the batch it was given
(i.e. zero cids resolved that round) and `request_failed` is `False`; resets to 0 the moment any cid
resolves or is confirmed unresolvable, so a batch still genuinely working through REData's queue is
never cut off early. See tests in
`dashboard/tests/hypothesis/test_resolve_deferred_pin_locations_no_progress.py`.

### Follow-up (same day): REData's active-request fallback - first attempt was wrong, corrected after live testing

The user separately asked REData to stop leaving an *actively-requested* cid stuck behind its own
30-day staggered floor for weeks - fine for a background prewarm sweep to wait that long, not fine
for a live caller blocked on `resolve-cids`'s response right now. First attempt (this session, same
day): a `GoogleLegacyCidLookupGateway` calling the legacy Place Details endpoint with
`place_id=cid:{cid}` - a real, if undocumented, convention for passing a bare Maps CID that this
session had reason to believe still worked. Shipped with full unit-test coverage (mocked HTTP) but
**never verified against a live Google API before being reported done** - a real gap, caught directly
by the user rebooting both services with the new code and pasting production logs showing every
single lookup failing with `INVALID_REQUEST`.

Live testing on both REData's and UrbanLens's real production API keys (REData's `diagnose_places_api`-
style probing plus UrbanLens's own pre-existing `manage.py diagnose_places_api` command, run live on
jungu) confirmed this decisively: `place_id=cid:{cid}` fails with `INVALID_REQUEST`/"Invalid 'placeid'
parameter"; the older bare `?cid=NUMBER` form (what UrbanLens's `GoogleGeocodingGateway.get_coordinates_by_cid`
used **before** REData's scrape-based resolution existed - confirming the user's recollection that this
used to work) now fails with `NOT_FOUND`/"The provided Place ID is no longer valid. Please refresh cached
Place IDs..." - Google's own wording for a real, external deprecation of old-style Place ID acceptance,
not a request-shape bug either agent could fix. Both UrbanLens's dormant fallback and REData's new one
were affected by the same dead mechanism; UrbanLens's had simply never been exercised in production
recently enough for anyone to notice.

**Corrected fix (REData)**: no working faster official API exists for a bare, never-before-resolved
CID - Places API (New) has no CID lookup at all. Replaced the dead paid-API gateway with a bounded,
forced *synchronous* run of the same real headless-Chromium scrape the async Celery path already uses
(`google_places.lookup.resolve_synchronously_for_active_request`), called directly from
`ResolvePlaceCidsView.post()` for a cid stuck behind `needs_refresh`. Capped at exactly 1 forced scrape
per request (`_MAX_FORCED_SCRAPES_PER_REQUEST`) with an 8-second timeout
(`_ACTIVE_REQUEST_TIMEOUT_MS`, vs. the background path's 20s default) - REData's gunicorn config
(`gunicorn.conf.py`) sets no explicit `timeout`, so its 30s default applies, and that browser
navigation is fully synchronous (not gevent-cooperative), meaning it blocks the *entire* worker
process, not just one request, for its duration; exceeding 30s would SIGKILL the worker mid-response,
dropping the whole batch rather than just leaving one cid pending. 1 call at an 8s cap leaves
comfortable headroom even in the worst realistic case. Removed the dead `GoogleLegacyCidLookupGateway`
and its `google_places_legacy_cid_lookup` rate-limit entry entirely rather than leaving unreachable
code behind.

## CRIS media on a multi-building campus is still only partial coverage (2026-08-05)

Fixed this session (see `plugins/builtin/cris_buildings.py`): the CRIS Media gallery was
returning nothing at all because `RedataGateway.fetch_cultural_resource_detail` handed back
REData's `{"detail_status", "resource"}` envelope while every caller read `attributes`/
`attachments` off it, plus three narrower mismatches (attachment `kind` compared as
`"PHOTO"`/`"DOCUMENT"` against REData's lowercase values, `resource_type` compared as
`"district"` against REData's `"building_district"`, and the *first* building of a lookup
being taken rather than the nearest one).

**Still outstanding**: a parcel-scope pin only aggregates the media of the single nearest
building plus the site-level record. CRIS's own authoritative "every building on this site"
list is a SURVEY resource's `USNs` roster - REData surfaces it via a resource's
`linked_resources`, and its own docs cite survey `12SD00541` as covering all 124 buildings of
the former Hudson River State Hospital campus. Following that roster (and using REData's bulk
`POST /cultural-resources/fetch-details/?lat=&lng=`, which UrbanLens's gateway does not
implement at all, to warm them within one provider's rate budget) is what would give a campus
pin the complete set. Deliberately out of scope of the bug fix: it needs a per-resource
fan-out with its own paging/rate story, not another field-name correction.

**Also worth checking operationally**: `fetch-detail/`, the bulk variant, and
`attachments/{id}/extract/` all require an API key holding `cultural_resources:write`, not
just `:read` - a read-only key 403s on all three and therefore yields zero attachments no
matter how correct this code is.

## `bun run build` fails as a package script on some hosts (2026-08-06)

Not a project bug, and not reproducible where it matters - but it costs an afternoon to
rediscover, so: on a host with Bun installed via `curl -fsSL https://bun.sh/install | bash`,
running the frontend build **through the package script** fails with

```
TypeError: Formats besides 'esm' are not implemented
```

...while the exact same build succeeds when the script file is invoked directly:

```bash
bun run bin/build-frontend.ts        # works
bun run build                        # fails
docker exec -w /app <app-container> bun run build   # works
```

Both Bun installs report 1.3.14, and the container (`oven/bun:1`) runs the identical script
happily - so this is something about how that particular Bun build executes a package.json
script, not about `bin/build-frontend.ts` or the `entries-classic` IIFE group it dies on.
Rewriting the script to use `Bun.build({format: "iife"})` instead of shelling out to
`bun build --format iife` does *not* help: the JS API accepts the format fine in a standalone
probe and still throws inside the package-script context, which is what rules the script
itself out as the cause.

**If you hit this, invoke the file directly or build in the container.** Do not "fix" the
build script - it is not what is broken.

## The plugin/panel extension surface, from an author's point of view (2026-08-07)

- **`docs/designs/plugins.md` does not exist.** It is at `docs/designs/plugins.md`, moved there by an
  earlier "clean, organize" commit that left every reference behind: CLAUDE.md (the project
  instructions themselves), `docs/FEATURES.md`, `docs/ROADMAP.md`, a design draft, and two source
  docstrings - seven dead paths, so an author following the instructions lands nowhere. Updated all
  seven to the real location rather than moving the file back, since the move looks deliberate.

- **The doc never shows how to write the panel.** Its worked example contributes
  `NpsPanelSource()` and then never defines it, so the one class an author actually has to write is
  the one part not demonstrated. Recorded rather than fixed here - writing that section properly is
  its own piece of work, and worth doing.

- **Three of a panel's required attributes fail quietly when omitted.** `section_id` and `title`
  default to `""` on the base class, and `cache_source` is meaningful only by convention. Get any
  of them wrong and nothing raises: you get a section with no DOM id for HTMX to swap against, a
  panel with no heading, or - the quietest - a cache-backed panel whose fetch writes one key while
  its read looks for another, so it polls forever and never renders. Added
  `panel_source_problems()`, reported once per key from `panel_sources()`, plus a test asserting
  every panel this repo ships is well-formed. That test is the useful half: it turns a silent
  runtime absence into a loud CI failure and keeps working as panels are added.

**The validation had to be calibrated against reality twice, which is the interesting part.** The
first rule demanded `section_id`/`title` of every panel and immediately flagged nine shipped media
panels. They were right and the rule was wrong: gallery media providers render as tabs *inside* the
combined Media gallery, which supplies the surrounding markup. The second rule exempted those and
flagged the core `boundary` panel - also correct, and also not a section: it fetches boundary data
the map and the external API consume, rendering nothing. The rule that survives is the precise one:
only `InfoPanelSource` and `SlidesPanelSource` render their own section, so only they need the
presentation attributes.

Had I "fixed" the nine panels the first run flagged, I would have added meaningless attributes to
correct code and called it an improvement. A validation rule asserted against the whole existing
codebase gets corrected by it; one asserted against a couple of hand-picked examples does not.

## Deleting your own photo silently broke it for everyone you shared it with (2026-08-07)

Found while auditing the quota feature (recently changed on this branch). The quota work
itself is sound - usage is always computed live rather than cached, so there is no staleness
bug; `materialize_media_item` really does stamp `EXTERNAL_MEDIA`; `_save_enriched_image`
attaches no profile so it is charged to nobody; and the quota is enforced on 15+ upload
paths. A scan for `Image`-creating functions with no quota guard returned only three, two of
which are correct by design.

The third was `create_pin_from_share`, and following it up found something worse than a
quota gap.

**The bug.** Sharing a pin copies its photos by assigning the *same* storage key
(`image=image.image.name`) - the bytes are deliberately not duplicated, so one file backs
several `Image` rows. But every deletion path called `image.image.delete(save=False)`, which
removes that file outright, with nothing checking whether another row still points at it.

So: share a pin, the recipient accepts, you later delete that photo from your own gallery -
and the recipient's copy becomes a broken image. No error, no log line, and the recipient's
row still exists so nothing looks wrong until the image fails to load.

Fixed with `services.media.images.delete_stored_file`, which removes the file only when no
other row references it, and takes an `also_deleting` set so a bulk delete does not count
rows inside its own batch as references. Routed all seven deletion sites through it
(pin gallery, wiki gallery, bulk gallery delete, organize queue, safety check-in, two
external-API endpoints, and the DM hard-delete task). Five tests, including two that guard
against over-correcting: an unshared photo's file must still be deleted, and the file must
go once the last row referencing it goes.

**Not changed, deliberately.** The recipient of a share is charged full quota for a file that
consumes no new storage, and share acceptance is the one `Image`-creating path with no quota
check at all - so accepting shares can silently push someone over quota, after which their
own uploads are refused with an error about photos they did not upload. Whether to exempt
those rows, charge them, or block the accept is a product/billing decision, and
`QuotaExemption` is explicitly scoped to "storage the whole community benefits from", which a
received share is not. Flagged rather than decided.

## Account deletion and the constraint-recreate class: both clean (2026-08-07)

Two checks this unit, both negative.

**The "recreate into a changed world" class is exhausted outside undo.** The four undo crashes
all came from recreating a row whose constraint slot had been taken since. The other creators
of `db_pin_unique_location_per_profile` handle it: `apply_pin_share_response` re-checks
`find_profile_pin_near_location` *inside* its `select_for_update` block and only creates when
nothing is there, and `accept_pin_suggestion` filters on `parent_pin__isnull=True`, matching
the partial constraint exactly. The undo handlers were the gap, not the pattern.

**Account deletion is deliberately designed, and the catastrophic case is avoided.** Every FK
pointing at `Profile` was enumerated. The split is coherent rather than accidental:

- **Personal data cascades** - pins, images, direct messages, labels, albums, notification
  logs, credentials, key material.
- **Contributions to shared or community space are `SET_NULL`** - wiki edits, wiki creators,
  aliases, links, owners, property sales, article revisions, trip creators and activities,
  fact evidence, trivia submissions, group chat creators. A departing user does not erase what
  other people are still using.
- **`Pin.source_share` is `SET_NULL`**, which is the one that matters most: a sharer deleting
  their account would otherwise cascade `PinShare` deletions into *recipients' pins*. It
  doesn't. `PinShare.parent_share` is `SET_NULL` too, so a provenance chain truncates rather
  than corrupting - `resolve_origin_share` simply ends its walk early.

### One asymmetry, surfaced rather than changed

`Comment.profile` is `CASCADE` while `TripComment.author` is `SET_NULL`. Both are comments a
user wrote in a space other people share, and deleting an account therefore erases your pin
and wiki comments while leaving your trip comments in place, authored by nobody. One of the
two is probably not what was intended, but which one is a data-policy question - whether
deletion means "erase what I wrote" or "keep the conversation readable" - and not a call to
make from inside an audit. Recorded here for the owner.

## E2EE group messages: the cryptographic membership boundary depends on the server (2026-08-07)

`models/e2ee/group_key.py` states the design claim plainly: "Versioning is what enforces
membership boundaries **cryptographically**" - a removed member "is excluded from every later
version, so messages sent after their removal are unreadable to them". The server-side half is
well built: `needs_rotation` is computed by comparing the latest version's envelope set against
active membership, and the key endpoint refuses to store a version whose envelopes don't cover
that membership exactly.

**But nothing validates the `key_version` a client sends a message with.**
`create_group_message` checks only `key_version < 1` (alongside the blob checks). It never
verifies that the version exists, belongs to this group, or is the current one. The value is
client-supplied and stored verbatim, on all four send paths - the WebSocket consumer, the
external API, the web controller, and the share-a-pin-in-a-group path.

So a message can be encrypted with a **pre-removal** key version that a removed member still
holds an envelope for. Reachable benignly - a tab open across the removal, an offline outbox
replaying queued messages, an API client caching the version it last fetched - and reachable
deliberately: a remaining member can choose an old version specifically to make a message
readable by someone the group ejected, and the server will accept it.

**What actually protects post-removal messages today is the server**, not the cryptography: a
removed member has no active membership, so `visible_window` and the active-membership checks
never serve them the ciphertext. That is a real defence and the messages are not currently
exposed. It is, however, exactly the dependency end-to-end encryption exists to remove - it
would not survive a database backup, a leak, a server compromise, or a future bug in the
delivery gate, which is the threat model the feature is written against.

### Why this is surfaced rather than fixed

The obvious fix - reject any `key_version` that isn't the latest - is **not sufficient on its
own**. If nobody has rotated yet, the latest version is still the pre-removal one, and its
envelopes still include the removed member. The rule that would actually hold the stated
property is stronger: refuse to accept an encrypted group message while `needs_rotation` is
true, i.e. until some client has stored a version whose envelopes match the active membership.

That trades availability for the property. Rotation is client-driven, so between a removal and
the next client rotating, group messaging would be blocked - and an offline outbox would have
messages rejected on replay and need re-encrypting. Whether that trade is right depends on how
strictly the removal boundary is meant to hold versus how tolerant the product should be of a
lagging or offline client, which is a decision for the owner rather than an audit. `docs/designs/e2ee.md`
already documents a related deliberate trade (recoverability over forward secrecy), so there is
precedent for either answer being the intended one.

## Filter-view defects cluster: triaged, 3 of 5 already resolved (2026-08-08)

Roadmap Tier-1 item 5 listed five defects and prescribed one agent owning the page. Static
triage shows the list is mostly stale:

- **Icon picker dead - already fixed.** `entries/saved-filter-detail.ts` exists solely to fix
  it, and its comment names the root cause: the page rendered the shared `_icon_picker.html`
  partial but never loaded anything defining `window.IconPicker`, so the trigger's onclick
  threw silently. The entry installs the global picker.
- **Badge picker parity - already fixed** (2026-07-23, browser-verified; see the label-picker
  extraction entry above). Both picker shapes now come from `shared/label-picker.ts`.
- **Preview doesn't refresh on criteria change - already fixed.** The detail page has a
  debounced live preview on form change/input with a supersession token (the same
  stale-response pattern this audit fixed in mention-autocomplete), and `_sfSaveRegions`
  dispatches a synthetic bubbling `change` precisely because property assignment fires no DOM
  event - region edits refresh the preview too.

**Polygon resurrection - mechanism identified, deliberately not blind-fixed.** The page's own
logic is correct: `draw:created/edited/deleted` all persist, and loading round-trips through
`_sfRegionLayers` properly. The resurrection is stock leaflet-draw semantics: delete mode is
transactional, click-deletions commit only via the sub-toolbar's small "Save" action, and
disabling delete mode (e.g. by clicking the polygon tool to draw next) **reverts** uncommitted
deletions - `draw:deleted` never fires, so the layers genuinely return, exactly matching the
report "deleted polygons resurrect on next draw". The fix is to stop using leaflet-draw's
remove tool (`edit.remove: false`) and implement immediate-commit deletion - a toggle that
removes a clicked layer from the feature group and calls `_sfSaveRegions()` at once. Not
shipped from this environment because it changes live map interaction behaviour, which needs a
real browser to verify; the roadmap entry carries the design.

**Page overflows footer** - CSS-level, needs a browser to reproduce; nothing checkable
statically.

## Where the audit stands (2026-08-08)

Every area reachable from this environment has now been covered. What remains needs something
this environment does not have:

- **The owner's decision**: share-quota treatment (#37), comment deletion semantics (#39),
  E2EE rotation enforcement (#40), the 12 unresolvable doc references, UL-34's vague repro,
  and unparking UL-277.
- **A browser**: the leaflet-draw immediate-commit deletion fix (designed, in the roadmap),
  the saved-filter page's footer overflow, and UL-353/UL-271's repro detail.

Nothing on the remaining task list clears the bar of "worth doing without those inputs" -
#38 would re-test surfaces verified correct, #32 is churn with no bug attached, #29's last
blocks are template-coupled or need a Leaflet stub. Stated per the standing instruction to
say so plainly rather than manufacture work.

## RESOLVED 2026-08-19: E2EE re-wrap recorded a KDF cost its stored blob was not made with

The original entry described the client trusting server-supplied Argon2 parameters. That half was
already fixed: `e2ee-client.ts`'s stale-password re-wrap derives with its own pinned
`KDF_OPSLIMIT`/`KDF_MEMLIMIT`, never `bundle.kdf_*`, and the comment there explains why it must (a
compromised server could otherwise answer `password_wrap_stale=true` with near-zero parameters and be
handed a cheaply-attackable blob).

What was left was the other end of the same change. `/rewrap` replaced `password_wrapped_secret` and
never touched `kdf_opslimit`/`kdf_memlimit`. Enrol deliberately accepts stronger-than-default
parameters and stores them, so a bundle enrolled above the floor kept advertising a cost its new blob
was not made with - and the read paths use `bundle.kdf_*`. Every later password unlock then derived
the wrong key: the device holding the cached key loops re-wrapping, a device without one loses the
password path entirely and has only the recovery key. Permanent, and reachable through the public API
by enrolling above the floor.

`/rewrap` now records the parameters alongside the blob, applying the same floor enrol does - so the
floor cannot be walked around one step later either. Absent parameters mean the **defaults**, not
"unchanged": the shipped client has always wrapped with its pinned constants here, and leaving the
bundle's own values was the bug. A recovery-only re-wrap replaces no password blob and so leaves them
alone. Guarded by `test_e2ee_kdf_floor.RewrapKdfParametersTests`.

## OPEN 2026-08-12: a password reset does not evict an intruder who minted an API key

Resetting a password invalidates every session (Django rotates the session auth hash), which is
what makes "reset your password" the standard response to a suspected compromise. It does **not**
touch `ApiKey` or django-oauth-toolkit `AccessToken` rows, and nothing else does either - there is
no revocation hook on password change anywhere in the codebase.

That matters because of a second gap: `controllers/api_keys.py::ApiKeyCreateView` mints a key
behind `LoginRequiredMixin` alone, with **no current-password proof**. So a session-only compromise
- a stolen cookie, a borrowed unlocked laptop - is enough to mint a long-lived credential, and the
victim's natural remedy does not remove it. The key keeps working with whatever scopes it was
given until someone notices it in the settings list and revokes it by hand.

Neither half is unusual on its own, and reasonable products differ (GitHub notifies rather than
revoking PATs on reset). What makes this worth recording is the asymmetry: this codebase *already*
demands a current-password proof for the three E2EE key-replacing endpoints - see
`test_e2ee_dual_auth.py::CurrentPasswordProofUnderCredentialAuthTests`, whose rationale is exactly
"an OAuth2 token grants send-and-read-messages, not replace this account's key material". The same
reasoning applies to minting a credential that can read the account's pins, photos and location
history.

**Not fixed here because both remedies are product decisions.** Revoking on password change is the
stronger option and silently breaks any legitimate integration the user has set up; requiring a
password proof to mint a key is the smaller change and matches the existing E2EE precedent, but it
is still a UX change to a settings flow. A middle option is to notify on both events, which this
app already has the notification machinery for.

## OPEN 2026-08-12: bulk-import paths skip the upload quota lock, which is fail-open anyway

`per_profile_upload_lock` exists because `quota_error_for_upload` reads current usage and the
caller creates the `Image` row afterwards - "N concurrent uploads from the same profile can each
pass the check before any of them commits". Its docstring tells callers to wrap the
check-then-create sequence in it.

Nine interactive call sites do (photo upload, DM attachments, article images, safety, tools,
visits, maps, consensus, photo uploads service). **Six do not**, and they are all the background
ones - `tasks.py` never imports the lock at all (four sites: Immich sync, Google Photos, and two
other fetch-and-store tasks), plus `services/pins/pin_suggestions.py` and
`services/import_export/import_data.py`.

Those are the paths where concurrency is *highest*: a bulk import fans out one task per image, so
many workers run the check for the same profile at once.

**Wrapping them is not the fix, which is why this is filed rather than done.** The lock is
deliberately fail-open - a caller that cannot acquire it logs a warning and proceeds - so under the
contention a bulk import actually produces, most workers would simply proceed without it. It
narrows the window for two near-simultaneous uploads; it does not bound a fan-out. Adding it to
these sites would look like protection while changing almost nothing.

The docstring already names the real fix: "true DB-level atomicity, which would need a dedicated
running-total column". A `Profile.storage_used_bytes` counter maintained by the same transaction
that creates the `Image` row would make the check exact for every path at once, and would also
remove the repeated `SUM(file_size)` scan that `get_storage_used_bytes` runs on each upload.
Sizing that (backfill, and keeping it correct across deletions and failed uploads) is a design
decision, not a refactor.

Fixed in passing: the lock released with a bare `cache.delete` guarded only by "did I acquire it",
so an upload slower than the 30s timeout - already having lost the lock to its successor - deleted
*that* upload's lock on the way out. It now uses the token-checked release from
`services.core.locks` (see the 2026-08-12 sweep-lock entry; same defect, same fix).

## LOW 2026-08-13: two more `get_or_create` sites state a uniqueness they do not enforce

Follow-up to the `Label` work, re-running the corrected sweep (one that understands functional
constraints, `field_id` vs `field`, related managers and `**kwargs` unpacking - the first version was
wrong all four ways). Besides `Label`, which is now fixed, two sites remain where the code's stated
intent is not backed by a constraint:

- `services/visits/safety.py:471` - `SafetyContactOptOut.objects.get_or_create(contact_profile, email,
  scope, owner, checkin)`. The docstring says these calls "don't create duplicate rows"; the model
  has no unique constraint, so two clicks on an opt-out magic link (or an email client prefetching
  it) can insert two.
- `services/import_formats/gpx_tracks.py:266` - `PinVisit.objects.get_or_create(pin, visited_at,
  source)`. Same shape: re-importing the same track is deduplicated by the `get`, but two concurrent
  imports are not.

**Neither is worth changing on its own evidence.** `blocks_notification` answers a boolean from
existence, so a duplicate opt-out row suppresses notifications exactly as one does; and a duplicate
visit needs two imports of the same track running at once. Recorded because both would be caught for
free by a `UniqueConstraint` if either model is touched for another reason, and because the *stated*
guarantee currently rests on nothing.

Not a finding: `services/facts/evidence.py:95` looked identical (`Fact.objects.get_or_create(key=...)`
against constraints on `('key','location')`, `('image','key')`, `('key','wiki')`) but supplies the
second half via `**lookup`, which the static sweep cannot see. It is correct.

## OPEN 2026-08-13: ~187 write routes have no test that names them

**Widened again 2026-08-16 (chunks 553-554): the sweep now reaches 486 of 647 named routes (75%),
up from 160.** Chunk 553's parameter measurement drove it - the cheap wins first (`label_kind`,
`profile_slug`, `profile_id`, `checkin_uuid`, `group_uuid`), then multi-parameter routes where every
parameter is known, then `session_id`, the single largest gate at 36 routes.

`session_id` needed a wrinkle worth recording: it names a **different model in each game** (SpotGuessr
`GameSession`, `TriviaSession`, `ConsensusSession`), so no single value satisfies all 36. The sweep
now accepts a *list* of candidate values for a parameter and tries each on single-parameter routes,
so every game family is exercised for real by one candidate and merely 404s for the others - and a
404 passes a sweep that only ever objects to a crash. Multi-parameter routes take the first candidate
of each, keeping the URL count linear.

Those 36 routes came back clean. The remaining 161 need `token`, `activity_id`, `album_slug`,
`round_id`, `image_id` and similar - each a fixture, each a further increment.

**Chunk 555: 532 of 647 (82%), and clean.** Six more fixtures - `album_slug` (14 routes), `token`
(9), `image_id` (8), `activity_id` (8), `alias_id` (6), `comment_id` (5) - each one object. No new
crashes.

That is the first widening increment to find nothing, which is worth noting rather than glossing:
the first three increments each bought a defect, this one bought none. The remaining gates
(`round_id`, `task_id`, `action`, `message_id`, `overlay_uuid`, `layer_uuid`) are smaller and need
more setup per route, so the cost per increment is rising while the yield has fallen. The sweep is
approaching the point where further widening is not the best use of effort - recorded so the next
person does not read 82% as an arbitrary stopping place.

**Extended 2026-08-16 (chunk 552), and it found two more.** The first version only reached routes
taking a single owned-object parameter - 160 of the resolver's 648 named routes. The larger
population was the **230 zero-parameter routes**, easy to overlook precisely because they need no
fixture: there is nothing to build, so nothing prompts you to build it. Sweeping those too turned up:

- **`test_ai` was a dead route.** `urls.py` wired `PinController.as_view({"get": "test_ai"})` to a
  method `PinController` does not have, so every request raised `AttributeError` - a guaranteed 500.
  Nothing in the codebase referenced it. This is the same class as the dead `google_images` route
  that `test_cross_user_route_access.py`'s docstring records finding; a second one had survived since.
  Removed.
- **`saved_filters.new` answered every POST with a 500.** `SavedFilterEditView` backs two routes, and
  its `post()` required `filter_uuid` while `new/` supplies none - so the TypeError fired before any
  application code ran. Not a broken user flow (the form posts to `saved_filters.create`; `new/` is
  only ever `hx-get`), which is exactly why it survived: no UI path exercised it. It now refuses with
  405, since editing without naming what to edit is not a request that view can answer.

`billing.stripe_webhook` also answers 503 in tests, and that is the endpoint **working** - it fails
closed when `UL_STRIPE_WEBHOOK_SECRET` is unset rather than processing an unverifiable payload. Named
in the skip set with that reason, alongside `logout`, which would otherwise end the session and leave
the rest of the sweep measuring login redirects.

Still out of reach: the 258 routes taking multiple parameters or a parameter this fixture set has no
value for. Stated here rather than hidden behind a green test.

**Partly addressed 2026-08-16 (chunk 551) - one property across all of them, rather than one test
each.** This entry says closing the gap route by route "is not a strategy". It is not; but a single
*property* asserted across every write route is, and
`test_write_route_smoke.py` now does that: logged in as the **owner**, it posts a minimal body to
every single-parameter owner-scoped route and asserts the answer is not a 5xx.

The property is deliberately weak - 400, 403, 404, 405 and 409 all pass, because refusing an empty
payload is correct and a generic sweep cannot know what any route is meant to *do*. Only "this
request made the server throw" fails. That is precisely the class this entry was opened for.

It complements rather than duplicates `test_cross_user_route_access.py`, which asks whether a
*stranger* gets in and flags only `200` - a crashing route answers 500 and passes it silently.

**What it found on its first run: exactly one crash, and it is the route that motivated this entry.**
`pin.link` raises `IntegrityError` on every request, which is the open detach-location product
decision. Nothing else in the sweep crashes. That is the instrument validating itself - it reproduced
the known bug from a standing start and produced no noise alongside it.

`pin.link` *was* exempted by name, with the exemption kept honest by
`test_the_known_crash_is_still_crashing`: when the product decision was made and the route fixed,
that test would fail and say so. **That is what happened.** As of 2026-08-18 the route answers 400
with an explanatory message instead of raising `IntegrityError`, and
`tests/hypothesis/test_write_route_smoke.py` now reads `_KNOWN_CRASHES: set[str] = set()` - the
allowlist is empty and every write route is held to the no-5xx property with no exceptions. (Read as
written, the paragraph above sent a reader to an exemption that no longer exists; the mechanism it
describes worked exactly as intended, which is the point worth keeping. An exemption nobody
re-checks is how an allowlist rots into a blindfold - chunk 546.)

This does not close the entry. The 186 routes still have no test asserting what they *do*; they now
have one asserting they do not crash.

Prompted by the detach 500 above, which survived because its route had no test while its
*sibling* route did.

*Updated 2026-08-14 (chunk 326):* `pin.link` itself is now covered - `test_pin_detach_location.py`
posts to it via `reverse()`, so the count is **186**. That is one route out of 187, which is the
honest scale of the dent: this entry describes a systemic gap, and closing it one route at a time
is not a strategy. What the detach case does show is the *unit* of progress - a single request
against a never-executed route was enough to pin a 500 permanently. Enumerating every route from the live resolver and matching each name exactly
against the test tree (exact match, because `pin.link` is satisfied in a naive grep by
`pin.link.delete`):

- 841 project routes (excluding Django admin and `oauth2_provider`)
- **301** never referenced by exact name in any test
- **187** of those accept `post`/`put`/`patch`/`delete`

Sampled five to check the number is real - `consensus.vote`, `dev_toolbar.toggle_theme`,
`external_api:messages.groups.read` have no test mention at all; `consensus.answer` and
`external_api:lists.resync` match only coincidental substrings in unrelated code
(`record_consensus_answer_evidence`, `lists_resynced`). All five are genuinely uncovered.

**Known false-positive mode, so treat 187 as an upper bound.** 92 test lines address endpoints by
literal path (`_BASE = "/dashboard/api/external/v1/labels/"`) instead of `reverse()`, and any route
covered only that way looks uncovered here - `external_api:labels` is one, and is well tested. The
other 1,920 URL references in the test tree do use `reverse()`, so the skew is bounded but real.

*Probed 2026-08-14 (chunk 338):* matching each route's static path prefix against the test tree
finds only **8** routes covered by literal path but not by name - so the literal-path
false-positive mode looks like a small correction, not a large one. Treat that as indicative
rather than decisive: the same probe enumerated 971 routes against this entry's 841 and 419
uncovered against its 301, so its route-set and namespace attribution differ from the careful
count above, and it searched only `dashboard/tests`. Where the two disagree, this entry's numbers
are the better ones.

**The authoritative instrument is `coverage.py`** (already installed, 7.15.0): run the suite under it
and report which view callables never execute. That answers the question directly instead of by
proxy, and is the right next step before anyone works through this list.

Worth doing because the one route from this set that *was* investigated - `pin.link`, the pin-detach
endpoint - turned out to fail with a 500 on every request (see the entry above). An untested write
route is not merely unverified; it is where a permanently broken feature can sit unnoticed.

## Database backups have no restore path, and their format defeats the only example

`core/controllers/backups/db.py` produces **plain-SQL** dumps: `pg_dump -U ... -f <path>`, no
`-Fc`, written as `backup_<YYYYMMDD>_<HHMMSS>.sql`. Creation, retention, scheduling, the atomic
temp-file rename, and (as of the 2026-08-14 audit chunk) reaping of abandoned `.tmp` files are all
implemented and tested.

Restoring one is not implemented, not documented, and not tested.

- No code path in `src/` or `bin/` restores a scheduled backup.
- The only `pg_restore` anywhere is the `infrastructure` repo's
  `bin/clone_prod_to_staging.sh:158` (moved there from this repo's own `bin/`
  since it was written; see `docs/OPS_TOOLING.md` there), which restores
  `/tmp/clone.dump` - a *different* dump that script creates for itself with its own flags. It has
  nothing to do with the backup directory.
- That mismatch is a trap rather than a mere omission. `pg_restore` **cannot read a plain-format
  dump**; it exits with *"input file appears to be a text format dump. Please use psql."* An
  operator under pressure, reaching for the repository's only restore example, hits that error on
  their first attempt at recovering production data.

Restoring these dumps actually requires `psql -U <user> -d <db> -f backup_....sql`, into a database
where PostGIS is already installed (a plain dump's `CREATE EXTENSION postgis` needs superuser, and
the dump does not create the database itself). None of that is written down anywhere.

Worth deciding deliberately rather than defaulting:

1. **Document the procedure** - the minimum. A `docs/BACKUPS.md` with the exact `psql` invocation,
   the PostGIS prerequisite, and whether to restore into a fresh database or an emptied one.
2. **Consider `-Fc`** (custom format). It compresses, allows selective/parallel restore, and makes
   `pg_restore` - the tool the repo already demonstrates - the correct one. This changes the
   filename suffix, so `BACKUP_FILENAME_RE`, `is_backup_temp_filename`, and any existing on-disk
   backups need handling together.
3. **Verify a restore at least once**, into a scratch database, ideally in CI against a seeded
   dump. Everything above is theory until a dump from this code has actually been restored.

Nothing here is a defect in the backup *writer*, which is careful. The gap is that the half of the
system that matters on the worst day has never been exercised.

---

## Session chat WebSockets have no rate limit or frame-size cap

`dashboard/consumers.py` accepts inbound frames on four sockets (`DirectMessageConsumer`,
`SafetyCheckinChatConsumer`, and the three game sessions via `_ParticipantSessionConsumer`). The
authorization on those sockets is thorough - participation is verified before any group is joined,
API-key scope is checked, credentials are re-validated on a timer. What is missing is anything
bounding *volume*.

- No per-connection or per-profile rate limit on `receive()`.
- No frame-size cap. `body` is truncated to `MAX_SESSION_CHAT_MESSAGE_LENGTH` (1000) only *after*
  the whole frame is read and JSON-parsed, so a multi-megabyte frame is fully processed before
  1000 characters of it are kept.
- Each accepted frame is one DB insert plus a channel-layer broadcast to every member of the
  group, so the cost is amplified by the number of connected participants.

This needs an authenticated, verified participant, which is what keeps it in "abuse by a member"
territory rather than an open vector - it is not remotely triggerable. But nothing stops a
participant from filling a session's chat table as fast as their socket allows, and the same
applies to DMs between accepted friends.

Two things to decide, both product calls rather than obvious defaults:

1. **The threshold.** Something like N messages per rolling window per profile per session.
   `services/core/rate_limiter.py` already exists for external API budgets; whether to reuse it or
   use a plain cache counter (`cache.incr` on a windowed key) is an implementation detail, but the
   limit itself is a judgement about what normal chat looks like.
2. **The response to exceeding it.** The consumers' established convention is an `{"type":
   "error"}` frame rather than a close - closing puts the client into a reconnect loop over a
   condition retrying cannot fix (that reasoning is already written down in
   `_ParticipantSessionConsumer.receive`). A throttle should follow it.

Both game chat and DM chat now funnel through shared code - `services/core/session_chat.py` for
games - so the limit can be implemented once per family rather than five times.

A frame-size cap is separately worth setting at the server: Daphne accepts
`--websocket_max_message_size`, which `docker-compose.yml` does not currently pass, so the
truncation above is the only bound and it happens too late to matter.

---

## The API rate limiter fails open, which uncaps spend rather than availability

`services/core/rate_limiter.check_rate_limit` returns `True` (allowed) when it cannot determine
whether a call is within budget. As of the 2026-08-14 audit chunk the handlers are narrowed to
`DatabaseError`, so a *bug* surfaces instead of silently reading as "allowed" - but the deliberate
fail-open on an actual database failure remains, and it is worth an explicit decision rather than
inheriting it.

This limiter is not a security control. It caps calls to **paid third-party APIs** (Google
Maps/Places, OpenAI, and the rest of `SERVICE_REGISTRY`), and the project already tracks a cost
estimate per call. So the failure mode of fail-open is money, not access: whatever quota or budget
the limits encode stops being enforced for as long as the failure lasts.

Two things make this less alarming than it first looks, and are worth knowing before anyone
"fixes" it:

- `record_api_call` calls `check_rate_limit` *inside* a `transaction.atomic()` block that has
  already run `ApiRateLimit.objects.select_for_update().get(service=service)`. A real database
  outage therefore raises at that line, before `check_rate_limit` is ever reached - so the
  fail-open path is much harder to hit from the main gateway flow than reading the function alone
  suggests.
- The path logs with `logger.exception`, so it is noisy rather than silent.

The decision to make: on a database failure, should a paid API call proceed unmetered, or should
it fail? Fail-closed protects the budget and degrades the feature; fail-open does the reverse.
Either is defensible - but it should be chosen, and the choice recorded here, rather than being a
side effect of an exception handler. If fail-closed is chosen, the same question applies to
`service_is_enabled`, which sits next to it in the same conditional.

---

## Colour values interpolated into `style="…"` - fixed at every entry point; the model fields still have no validators

**Superseded note (corrected 2026-08-14).** An earlier version of this entry listed
`markup-engine.ts:66/84/150` and `markup-toolbar.ts:297/299` as unfixed because a hex-only
validator might blank a legitimate `rgba()`/`none` value. That was half wrong, and the correction
is worth keeping:

- `markup-engine.ts` was **already safe**. It defines its own `safeColor(v, fallback)` and
  `safeOptionalColor` (which returns `"none"` unchanged), and runs the value through them before
  interpolating - e.g. `const color = safeColor(s.color, "#e53e3e")` on the line above the
  `style="color:${color}"` that the grep flagged. The flagged lines were reading
  already-sanitised locals.
- `markup-toolbar.ts` was **not** safe and now is. It imported no validator and interpolated
  `item.color` and `textBackground()`'s `item.border_color` straight into `style="…"`. Those are
  fixed with the shared `shared/color-safety.safeColor`; the `"none"` case that made this look
  risky is handled explicitly, exactly as `markup-engine.safeOptionalColor` already did.

Markup colours are *less* validated than label colours server-side, which is what made this worth
chasing: `MarkupShape.color` is `CharField(max_length=20, default="#e53e3e")` and `border_color`
`CharField(max_length=20, blank=True)` - **no `choices` at all**. `x" onmouseover="a` is 17
characters.

Not changed, and deliberately: the colours passed to Leaflet as *options*
(`markup-toolbar.ts:318, 345, 348` - `fillColor:`, `color:`) are set programmatically as style
properties rather than interpolated into markup, so an invalid value is inert there rather than
injectable.

### The server-side half - RESOLVED 2026-08-14

`services/core/colors.clean_color` now validates every colour write path (32 of them, across
`controllers/labels.py`, `external_api/views.py`, `controllers/markup.py`,
`controllers/detail_pins.py`, `controllers/maps.py`, `controllers/custom_layers.py` and
`controllers/saved_filters.py`). Eight further sites in `controllers/detail_pins.py` were missed on
the first pass and fixed on 2026-08-14: the original sweep matched `color = X.get(...)` assignments
but not dict-literal `"color": body.get(...)` entries, and its field list was built from request
keys, so `detail_bg_color` (populated from `bg_color`) never appeared. Invalid input is coerced to
each call site's existing default
rather than raising, since these come from palette pickers and a non-colour is a malformed
request; `"none"` is permitted only where it means "no border".

Left for a future pass: the model fields themselves are still permissive
(`MarkupShape.color`/`border_color` have no `choices`, `Label.color` has choices Django will not
enforce on `save()`). Validation now happens at every known entry point, but a new write path
added without `clean_color` would reintroduce the gap. A `validators=[...]` on the fields, or a
custom field type, would make it structural rather than conventional.

---

## Inline template JavaScript is structurally untested

`pages/map/index.html` contains several thousand lines of JavaScript inside a single `<script>`
block. `bun test` covers `frontend/ts/`, so none of it can be imported, mocked or exercised - the
helpers added there in the 2026-08-14 audit had to be verified by extracting the functions into a
scratch file and running them separately.

This is why bugs like the two above survive: the code is invisible to every automated check the
project has. Moving that script into `frontend/ts/` (where it would get `tsc --noEmit`, bun tests,
and the same review as the rest of the frontend) is a large job, but the map page is the single
biggest concentration of untested logic in the codebase.

---

## Inline template JS: 21,543 lines, 14 escaping helpers, zero test coverage

Measured 2026-08-14. `dashboard/templates/` contains **21,543 lines of inline JavaScript across
101 templates**, versus 22,684 lines in `frontend/ts/` which `tsc --noEmit` and 394 bun tests
cover. Half the frontend is outside every automated check.

Concentration (top 5 = 49% of the total):

| lines | template |
|---|---|
| 5,175 | `pages/map/index.html` |
| 1,772 | `pages/messages/index.html` |
| 1,377 | `pages/trips/detail.html` |
| 1,294 | `pages/location/index.html` |
| 1,118 | `themes/base.html` |

The concrete cost, beyond "untested": 44 function names are defined in more than one template,
including **14 HTML-escaping helpers under 9 names**, of which 6 escape `&<>` only and 8 also
escape quotes. Nothing in any of the names distinguishes the text-node case from the attribute
case, and the 2026-08-14 audit found two real bugs that existed precisely because the wrong one
was in reach (`memories/index.html`, `map/index.html`).

Suggested order of work, largest payoff first:

1. **Move `pages/map/index.html`'s script into `frontend/ts/`.** One file, 5,175 lines, ~24% of
   the problem, and the page where the audit found the most issues.
2. **Add `frontend/ts/shared/escaping.ts`** exporting `escapeText` and `escapeAttr` (names that
   say which context they are for), and have migrated code import it rather than redefine it.
3. Migrate the next four largest templates.

This is a large job and nothing above is urgent in isolation. It is recorded because every future
bug of this shape in these files will be invisible to CI, and because the duplication means fixing
one instance fixes nothing else.

---

## Nine named routes with no discoverable caller (candidates for review, not confirmed dead)

From a 2026-08-14 sweep of all 753 named routes. 61 have no static reference outside `urls.py`;
30 are reached via `reverse(f"{prefix}.{suffix}")` and 34 live in `external_api/urls.py` where the
callers are API clients. `password_reset_complete` is Django's own. That leaves:

- `add_review`
- `comment.locations`
- `dev_toolbar.toggle_map_dark_mode`
- `label.index`
- `location.wiki.article.restore`
- `location.wiki.article.revision`
- `location.wiki.gallery.image`
- ~~`pin.upload.takeout`~~ - RESOLVED 2026-08-14: superseded duplicate of the
  `pin.import.preview`/`confirmed` wizard flow; handler and route removed
- `safety.checkin.gallery.image`

**Do not bulk-delete these.** Each needs checking individually, because the plausible explanations
differ: `dev_toolbar.toggle_map_dark_mode` is dev tooling that may be invoked by hand;
`pin.upload.takeout` and the two `gallery.image` routes may be hit as literal URLs built in inline
template JavaScript (which this audit has separately measured at 21,543 untested lines, so it is
exactly where a hardcoded path would hide); `location.wiki.article.restore`/`revision` pair with
`services/wiki/articles.restore_revision`, which does exist, suggesting a wired-up feature whose
entry point is somewhere the scan could not see.

A route with genuinely no caller is still worth removing - it is surface area that has to be kept
authorised and tested - but "the grep found nothing" has produced a false positive in every
category this sweep touched.

---

## 46 BEM modifiers applied in templates with no CSS rule

Measured 2026-08-14 against the compiled `style.css` (current at the time - no `.scss`
was newer). Each base class *is* styled, so each of these was written to create a visual
distinction that does not render. Not fixed, because what each should look like is a design
decision. Sorted by how many templates apply it.

| modifier | templates | first use |
|---|---|---|
| `card--secondary` | 8 | `pages/location/index.html` |
| `badge--muted` | 5 | `pages/site_admin.html` |
| `card--primary` | 3 | `pages/location/index.html` |
| `ul-game-hud__group--lead` | 3 | `pages/consensus/index.html` |
| `dm-composer-attachment-chip--share` | 2 | `partials/messages/_group_thread.html` |
| `form-row--map` | 2 | `pages/safety/create.html` |
| `btn--sel` | 1 | `pages/organize/index.html` |
| `btn--trigger` | 1 | `partials/ui/_icon_picker.html` |
| `btn-icon--primary` | 1 | `pages/site_admin_ui_components.html` |
| `cf-value-input--reference` | 1 | `partials/custom_fields/_value_input.html` |
| `cf-value-input--select` | 1 | `partials/custom_fields/_value_input.html` |
| `cf-value-input--url` | 1 | `partials/custom_fields/_value_input.html` |
| `comment-reply-btn--sm` | 1 | `partials/trips/trip_comments_panel.html` |
| `detail-item--abandoned` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--built` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--coordinates` | 1 | `partials/pins/pin_overview_partial.html` |
| `detail-item--last-active` | 1 | `partials/pins/pin_overview_partial.html` |
| `dm-composer-attachment-chip--map` | 1 | `partials/messages/_thread.html` |
| `dm-thread--group` | 1 | `partials/messages/_group_thread.html` |
| `form-row--maps` | 1 | `pages/safety/detail.html` |
| `form-row--message` | 1 | `pages/safety/create.html` |
| `form-row--plan` | 1 | `pages/safety/create.html` |
| `form-row--time` | 1 | `pages/safety/create.html` |
| `form-row--title` | 1 | `pages/safety/create.html` |
| `fp-cf-input--select` | 1 | `partials/custom_fields/_filter_input.html` |
| `fp-cf-input--text` | 1 | `partials/custom_fields/_filter_input.html` |
| `home-widget--stats` | 1 | `partials/home/_widget_stats.html` |
| `inline-sub-form--pricing` | 1 | `pages/site_admin_subscriptions.html` |
| `map-overlay-btn--cancel` | 1 | `partials/layout/_map_annotations_panels.html` |
| `notif-item__icon-wrap--pin_shared` | 1 | `partials/notifications/notification_item.html` |
| `notif-item__icon-wrap--safety_ci_due` | 1 | `partials/notifications/notification_item.html` |
| `notif-item__icon-wrap--visit_suggested` | 1 | `partials/notifications/notification_item.html` |
| `org-bulk-btn--edit` | 1 | `pages/organize/index.html` |
| `org-bulk-btn--merge` | 1 | `pages/organize/index.html` |
| `page-onboarding--wiki` | 1 | `pages/location/wiki.html` |
| `sv-img--fallback` | 1 | `pages/location/street_view.html` |
| `trip-map-marker-num--ghost` | 1 | `pages/trips/detail.html` |
| `trip-panel-empty--all-completed` | 1 | `partials/trips/trip_activities_panel.html` |
| `trip-panel-empty--tab` | 1 | `partials/trips/trip_activities_panel.html` |
| `ul-game-hud__btn--focus` | 1 | `partials/games/_game_hud_controls.html` |
| `visit-item--pending` | 1 | `partials/pins/_visit_history.html` |
| `visit-list--pending` | 1 | `partials/pins/_visit_history.html` |
| `visit-source--pending` | 1 | `partials/pins/_visit_history.html` |
| `wiki-seed-list--aliases` | 1 | `partials/pins/pin_wiki_create_dialog.html` |
| `wiki-stat-row--composite` | 1 | `partials/pins/_wiki_stat_rating_item.html` |
| `wiki-stat-row--mine` | 1 | `partials/pins/_wiki_stat_rating_item.html` |

Worth triaging rather than doing wholesale: `ul-game-hud__group--lead` (the leading
score on all three game pages), the three `visit-*--pending` classes (a pending visit is
indistinguishable from a confirmed one) and the `notif-item__icon-wrap--*` set are user-visible
states; others are cosmetic hierarchy that may simply have been abandoned. Deleting the class
from the template is as valid a resolution as writing the rule.

---

## 1,217 statements of write handlers that no test executes

Measured 2026-08-14 with `coverage.py` over the full suite; full list in
`docs/reports/2026-08-14-view-coverage.md`.

The view layer is 80% covered by statement, which sounds healthy. The shape underneath is less so:
**208 of 1,795 callables never execute**, and **100 of those are `post`/`delete`/`put`/`patch`
handlers totalling 1,217 statements**. Half of the unexercised view code is code that mutates data.

Suggested order, highest risk first (statement counts in brackets):

1. `controllers/labels.py::LabelBulkConvertView.post` [36] and `LabelBulkEditView.post` [33] -
   bulk mutations over many rows, and the label subsystem has already produced several bugs.
2. `controllers/site_admin.py::SiteAdminUsersView.post` [35] - user administration.
3. `controllers/detail_pins.py::LocationWikiDetailPinEditView.post` [34] - wiki-scoped edits, which
   touch the place-domain visibility rules.
4. `controllers/albums.py::AlbumEditView.post` [31], `consensus.py::ConsensusPhotoUploadView.post`
   [31], `visit_suggestions.py::VisitSuggestionRespondView.post` [31],
   `calendar_sync.py::CalendarImportView.post` [30].

`controllers/pin.py::PinController.upload_takeout` [39] is a special case: it is also on the
caller-less route list above, so it should be resolved (deleted or tested) before anything else -
two independent signals agree that nothing reaches it.

Caveats worth keeping attached to this number: coverage measures execution, not correctness, and
the run was scoped to `controllers/` and `external_api/`, so a service called by an uncovered
handler may itself be well tested.

---

## The category-on-pin methods have no production callers

Categories became a `Label` kind (`KIND_CATEGORY`), managed generically by the organize/bulk-edit
paths (`controllers/pin_bulk.py`, `controllers/pin_suggestions.py`). The older per-pin category
helpers were left behind:

- `Pin.change_category()` - after the 2026-08-14 removal of `MapController.change_category` and its
  route, zero callers outside tests
- `Pin.add_category()` - zero callers outside tests
- `Wiki.add_category()` - zero callers outside tests

Each still has tests, so the suite currently exercises code nothing reaches. That is worse than
either extreme: the tests give the appearance of coverage, and any bug found in these methods reads
as a production bug when it is not.

**A correction that belongs with this.** During the label-uniqueness work earlier the same day, a
`MultipleObjectsReturned` bug was found and fixed in `add_category` (the `Label.objects.get_or_create`
lookup was missing `profile=None`, so a case-insensitive match could return both a global and a
personal label). The fix is correct and the tests are real, but the method has no production caller
- so that bug was **not reachable in production**, and it was reported at the time without that
qualification.

Deciding what to do needs a product call rather than a mechanical one: either delete the three
methods with their tests, or wire them back up if per-pin category assignment is still wanted as a
distinct concept from labels. Deleting is the more likely answer, since `KIND_CATEGORY` labels
already do this and have a UI.

---

## API behaviour change: non-hex colours are no longer stored

From 2026-08-14, every colour write path runs through `services/core/colors.clean_color`, which
accepts `#rgb`/`#rrggbb` (plus the literal `none` where a border legitimately means "no border")
and otherwise falls back to the field's default.

This is a **visible change for external API clients**. `PATCH /labels/<uuid>/`,
`POST /labels/bulk/edit/` and the saved-filter endpoints previously stored whatever string was
sent - `{"color": "red"}` was accepted and persisted, and one test asserted exactly that.

It was never a working value:

- `Label.color` declares `choices` that are all hex; `choices` is enforced by `full_clean()`, which
  `save()` does not call, so the invalid value was stored rather than rejected.
- Every renderer appends an alpha suffix (`color + "33"`), so `"red"` became `red33` - not a
  colour, painting nothing. The chip rendered as though no colour had been set.

So the change replaces "stored, then silently ignored by the UI" with "not stored". Clients sending
named CSS colours will see the field come back empty rather than echoing their input.

Worth deciding, and not decided here: whether these endpoints should **reject** an invalid colour
with a 400 rather than coercing it. Coercion matches the HTML form paths (where the value comes
from a palette picker and anything else is a malformed request), but an API arguably owes its
callers an error instead of silent data loss. If that changes, it should change for all 31 sites at
once, in `clean_color`'s callers rather than in the helper.

---

## Two dead queryset methods

`Pin.by_category()` and `Wiki.by_category()` have no callers anywhere - Python, templates or tests.
Both filter `labels__name=<category>` without `distinct()`, so they would return duplicates if used.

Found while tracing the multi-valued-filter candidates (2026-08-14); the rest of that list resolved
- 7 already collapse via `filter_by_criteria`, 2 cannot duplicate (`__isnull=True` tests for the
absence of related rows), and 3 (`rated`/`rated_over`/`rated_under`, test-callers only) were given
the `distinct()` they were missing.

Filed rather than deleted because removing public queryset API is a judgement about whether it is
scaffolding for something planned. If it is not, both should go - dead API with a latent bug is the
worst combination, since the next caller inherits the bug.

---

## Queryset API with no production caller: 70 of 251 (candidate count)

From a 2026-08-14 sweep of every public method on a `*/queryset.py` class:

- **44** are not called from any file other than the one defining them
- **26** are called only from tests

That is 28% of the queryset API with no production consumer, which is worth a look - a custom
queryset method exists to be the one place a piece of domain logic lives, and one nothing calls is
either scaffolding, a leftover, or a piece of logic that got reimplemented inline somewhere else.
The last of those is the interesting case, because it means the same rule now exists twice.

**Known false-positive class, do not treat the 44 as dead.** The scan deliberately ignores calls
within the defining file, so a method used only by its siblings is flagged. `apply_label_groups` is
in the list and is definitely used - `filter_by_criteria` calls it, in the same file, as verified
while tracing the duplicate-row candidates. Such methods are arguably mis-scoped (a `_`-prefixed
helper rather than public API) but they are not dead.

Two entries are confirmed dead by separate inspection: `Pin.by_category` and `Wiki.by_category`
have no callers anywhere, in any file, including their own.

Worth doing properly with a call-graph rather than a name grep, since the test-only 26 in
particular may be exercised through the very `filter_by_criteria`-style aggregators that make them
look unused.

---

## RESOLVED 2026-08-19: label-kind literals - and the blind spot in the scan that found them

`models/labels/meta.py` defines `KIND_TAG`/`KIND_CATEGORY`/`KIND_STATUS`/`KIND_USER`/`KIND_MEDIA`.
Every production call site now uses them. What is left below is the part worth keeping: **the scan
that produced the original list of twenty could not see six of the sites.**

It resolved choices via `Model._meta.get_field(name).choices`, which structurally cannot follow a
lookup traversal - so `labels__kind="status"` was invisible to it, and four such sites survived
(`models/pin/queryset.py` twice on `status` plus once on `category`, `models/wiki/queryset.py` on
`category` and `tag`). It also missed two plain `Label(kind=...)` creates (`tasks.py`,
`services/apis/locations/google/maps.py`) that it should have caught. All six were fixed on
2026-08-19; the two `models/pin/model.py` sites the entry recorded as "the two that remain" had
already been done before that.

The entry's own stated risk applied most sharply to exactly the sites it could not see: a filter on
a stale literal silently matches nothing, where a create with one silently writes a value nothing
queries. **A future sweep of this class must grep `related__field=` traversals separately** -
`_meta`-driven scans of this shape will keep reporting a clean list while missing them.

Remaining bare literals are deliberate and must stay: `baker_recipes.py` (test fixtures) and
`migrations/` (frozen history - a migration that imports a constant changes meaning if the constant
does).

## CORRECTION: the `to_json()` prefetch work does not affect the map

Claimed repeatedly during the 2026-08-14 audit, and wrong: that fixing `Pin.to_json()`'s prefetch
behaviour reduced the map's per-pin query cost.

**The map does not use `Pin.to_json()`.** Its payload is built by `services/map_pins/payload.py`,
which annotates the rating with a `Subquery`:

    latest_rating = Review.objects.filter(pin_id=OuterRef("pk")).order_by("-created").values("rating")[:1]
    ... .annotate(map_rating=Subquery(latest_rating), child_count=Count("detail_pins", distinct=True))

That is already query-flat and does not touch `Pin.rating` or `Pin.to_json()` at all. The map was
never paying the cost that was measured.

What the work actually did:

- **`Pin.to_json()`** has **no production callers** - the only textual match outside tests is a
  *comment* in `pin_lists.py:100`. The measured 4-queries-per-pin was real, and was being paid by
  nothing. The method is now correct if it is ever used, which is worth something but is not a
  performance win.
- **`Pin.rating`** *is* live: `models/pin/serializer.py:73` exposes it, so the DRF path pays one
  query per pin unless the caller prefetches `reviews`. The `.latest()` -> `max(...)` change makes
  that path *capable* of being flat; whether the serializer's queryset prefetches `reviews` was not
  checked, and is the actual open question.

The measurement was sound; the attribution was not. A query-count test over a method proves what
that method costs, not that anything calls it - and `to_json()` looked like the map's serialiser
because a comment elsewhere said so.

---

## `Image` carries labels but does not inherit `LabelledModel`

`Pin` and `Wiki` both inherit `abstract.LabelledModel`, which supplies `categories`/`tags`/
`statuses` as prefetch-friendly properties over `self.labels.all()`. `Image` declares its own
`labels = ManyToManyField(...)` and does not inherit the mixin.

**Not a defect.** Checked 2026-08-14: `Image` does not reimplement those accessors badly - it does
not have them at all, and no code filters an image's labels by kind inline. Nothing is paying a
per-row query because of this.

It is an inconsistency worth resolving *if* image label access grows: the next person needing
"an image's media labels" will write `image.labels.filter(kind=...)`, which bypasses any prefetch,
rather than inheriting the version that does not. That is precisely how `Pin.to_json()` acquired
the bug fixed earlier the same day.

Care required if adopted: `LabelledModel` may declare the `labels` field itself, in which case
`Image`'s own declaration has to be reconciled rather than simply adding the base class - a
migration question, not a refactor.

---

## Workflow: Django logic can be checked on the host without the container

Found 2026-08-14 (audit chunk 320-321). `CLAUDE.local.md` correctly notes that pytest needs the
`app` container, because the project's settings import GeoDjango and this host has no GDAL. That
is true, and it is easy to over-generalise into "no Django at all on the host", which is false.

`django.conf.settings.configure(...)` + `django.setup()` builds a minimal Django without loading
the project settings, so nothing imports `django.contrib.gis`:

```python
from django.conf import settings
settings.configure(USE_TZ=True, TIME_ZONE="UTC", DATABASES={}, INSTALLED_APPS=[])
django.setup()
```

Useful for anything not touching the ORM or geo models - timezone behaviour, template filters,
form/field validation logic, signal wiring, pure service functions. Seconds instead of a
multi-minute container cycle.

It caught a real error this way: a test asserting `date.today()` is unaffected by
`override_settings(TIME_ZONE=...)`, which is wrong because Django's `setting_changed` receiver
also rewrites `os.environ["TZ"]` and calls `time.tzset()`.

## Note: NotificationLog receivers self-guard on `created`

Recorded 2026-08-14 (audit chunk 343) because it is easy to get backwards. The three
`NotificationLog` `post_save` receivers - `push_notification_to_browser`, `enqueue_text_alerts`,
`enqueue_native_push` - all begin `if created and instance.profile_id`. Marking a notification read
via `queryset.update()` is therefore safe for a reason that has nothing to do with `update()`
skipping signals: a plain `save()` would be equally safe. Anyone converting those call sites to
`save()` for signal-correctness reasons should know they are not fixing a re-push hazard, because
there isn't one.

## Note: inline template JS defeats source searches of `frontend/ts`

Recorded 2026-08-14 (audit chunk 347) as concrete evidence for the inline-JS migration item.
Searching `dashboard/frontend/ts` for readers of the `ul_pins_dirty` cache-invalidation flag returns
**zero production hits**, which reads as a dead code path. The actual consumer is inline JS in
`templates/dashboard/pages/map/index.html` (lines ~1444 and ~2012), and two of the five writers are
inline in other templates.

Any audit, refactor, or dead-code sweep scoped to the TypeScript tree will draw wrong conclusions
about client behaviour while looking thorough. Until the inline JS is migrated, searches for
client-side behaviour must cover `templates/**/*.html` as well.

## PARTIAL 2026-08-14: the dev stack's `app` container has been unhealthy for its entire uptime (one branch resolved)

Found by a runtime check (audit chunk 351) rather than by reading code - the first finding in this
audit that no static analysis could have produced.

```
urbanlens_devs1_app   Up 10 days (unhealthy)   FailingStreak=23150
curl http://localhost:$UL_APP_PORT/dashboard/login/  ->  HTTP 000 (connection refused)
```

**The streak count matters for attribution.** 23,150 consecutive failures is on the order of the
container's whole 10-day uptime, so this is not a side effect of this session's activity (a
70-minute test suite and repeated `docker cp` into the same container), which was the obvious
suspicion and is wrong.

What still works: Django itself runs fine *inside* the container - the full 10,781-test suite
executed there. So this is the serving/healthcheck path, not the application code.

Two consequences worth noting:

- `nginx` reports **healthy** while `app` does not, even though `CLAUDE.local.md` documents nginx as
  waiting on `app`'s healthcheck. Either the dependency is not actually gating, or it gated once at
  startup and never re-evaluated. Both are misleading in the same direction: the stack *looks*
  serviceable.
- The documented workflow - "full stack reachable at `http://localhost:$UL_APP_PORT` once healthy" -
  cannot succeed in this checkout. Anyone following it gets a refused connection with no error to
  read, since the app log has been silent for at least 6 hours.

Also noticed: `.env` has `UL_APP_PORT=21810`, while `CLAUDE.local.md` states this slot's port is
21811. One of the two is stale; the connection is refused on 21810 regardless.

**Diagnosed one level further (chunk 352).** The cause is not a missing route or a dead process:

- healthcheck is `curl -f http://localhost:8000/health/`;
- the `health/` route **exists** (`UrbanLens/urls.py:109`, `HealthController.check`);
- `manage.py runserver 0.0.0.0:8000` **is running** (two processes - the reloader parent from Aug 04
  and a child);
- yet `curl` from *inside* the container returns **HTTP 000 for both `/health/` and `/`**.

So the dev server is wedged: alive, consuming CPU, not accepting connections. That rules out the
three cheap explanations (route missing, process crashed, port misconfigured) and leaves a genuine
hang.

One complication for anyone picking this up: the child `runserver` process restarted at 00:33 today,
which is when this session began `docker cp`-ing source into the container - the autoreloader will
have fired on those syncs. The **10-day failing streak predates all of that**, so the wedge is not
caused by the syncs, but the *currently running* process is one they restarted. A clean
`docker compose restart app` is the first thing to try, and would also confirm whether the wedge
reproduces from a fresh start.

**Pinned precisely (chunk 353): port 8000 is never bound.** Reading `/proc/net/tcp` inside the
container, no socket is listening on 8000 (hex `1F40`); the only listening port is `0xAA29` (43561),
an ephemeral socket. So `runserver` is not hung *serving* requests - it has never reached
`bind()`. With ~33 minutes of accumulated CPU on the child process, it is stuck **before** the
server starts: imports, system checks, migration checks, or the staticfiles/frontend build the
Dockerfile runs at boot.

That narrows the search a great deal. The next step is not networking - it is finding what runs
before the bind and can block indefinitely. `docker exec ... py-spy dump --pid <child>` (or
`faulthandler`) would name the exact frame.

**Chunk 354 complicates that story - record the evidence, not a tidy narrative.** Both processes are
`S (sleeping)` with `wchan 0` (an ordinary sleep, not blocked in a kernel call), each with **8
threads**, and the child has accumulated ~33 minutes of CPU since 00:33 - roughly 3% sustained.

That does not fit a single blocking call during startup:

- a process stuck early in imports would not have 8 threads;
- a one-time hang would not burn CPU steadily for 18 hours.

A polling loop fits better - Django's `StatReloader` scans every file each second, and a
crash-reload-crash cycle would keep the inner server from ever holding a binding. But **the app log
has been empty for at least 6 hours**, and a repeatedly crashing `runserver` should be printing
tracebacks. Either output is not reaching `docker logs`, or nothing is crashing.

**Resolved one branch (chunk 355).** Sampling `/proc/<pid>/stat` utime+stime twice, five seconds
apart, the child consumes **11 ticks in 5s - about 2.2% of one core, sustained**. The process is
genuinely *working*, not blocked. That eliminates the single-blocking-call explanation and matches
a poll loop, which is consistent with the ~33 minutes of CPU accumulated over 18 hours.

So the state is: **actively looping, never binding, silent logs.** The most likely remaining
explanation is Django's `StatReloader` polling while the inner server process fails to start or
repeatedly exits - the reloader survives, the child never holds port 8000. Under that reading the
silent logs are the anomaly to chase, since a failing child should print something.

`py-spy dump` on both pids would still name the frame in about a minute, and is the recommended next
step. Note that `/proc/<pid>/io` is not readable even via `docker exec -u root` here, so measure CPU
via `/proc/<pid>/stat` fields 14+15 rather than IO counters.

## OPEN 2026-08-14: the documented `docker cp` resync breaks the app container

**Root cause of the unhealthy-container entry above.** `CLAUDE.local.md` documents

```
docker cp src/urbanlens/. urbanlens_devs1_app:/app/src/urbanlens/   # resync without a rebuild
```

as the way to sync host changes into the container. The host tree contains
`src/urbanlens/logs/`, owned by the host user `apps` (**uid 568**). The container's app runs as
`appuser` (**uid 1001**). `docker cp` preserves the *source* ownership, so every resync hands the log
directory to uid 568 with mode `rw-rw-r--` - no write bit for others - and `appuser` can no longer
open it.

Django's logging config then fails at startup:

```
PermissionError: [Errno 13] Permission denied: '/app/src/urbanlens/logs/django.log'
ValueError: Unable to configure handler 'file'
```

which raises **before** `runserver` binds. That accounts for every symptom recorded above: no
listener on 8000, silent `docker logs` (the file handler never configures), sustained ~2% CPU (the
autoreloader retrying), and a process that is sleeping rather than blocked.

**Why it took so long to see.** `docker exec` defaults to **root**, so every diagnostic and every
`pytest` run in this session wrote to that log file successfully - `django.log` had a fresh
timestamp minutes before the investigation, which reads as "permissions are fine" and is the exact
opposite of the truth for the process that matters.

Ownership has been restored (`chown -R appuser:appuser /app/src/urbanlens/logs`), but the running
`runserver` will not recover on its own - it needs `docker compose restart app`.

**The workflow itself still needs fixing**, or the next resync reintroduces it. Options: exclude
`logs/` from the copy, `chown` after every `docker cp`, move the log directory outside the synced
tree, or make the log path configurable so the container writes somewhere it owns. Until then the
documented command should carry the `chown` as a second line.

## OPEN 2026-08-14: the dev database is 18 migrations behind the code

Found by reading Celery worker logs (audit chunk 359) - another finding no source analysis could
produce.

`manage.py showmigrations dashboard` inside `urbanlens_devs1_app` reports **18 unapplied
migrations**. The consequence is already visible in the worker log, 16 `django.db.utils.
ProgrammingError`s dated **2026-08-04**:

```
column dashboard_wikis.officially_created does not exist
column dashboard_site_settings.public_costs_page_enabled does not exist
column dashboard_profiles.photo_taking_preference does not exist
```

Same date the `app` container went unhealthy, so the two may share a cause (a boot sequence that
stopped part-way through migrate/collectstatic) or merely a trigger.

**Why the test suite cannot catch this.** pytest builds a *fresh* database from the migration files,
so a full green suite - 10,781 passing, run today - says nothing about whether the long-lived dev
database has had those migrations applied. The two are independent, and only the dev DB serves the
running app.

Fix is `manage.py migrate` in the container, but check first whether the boot sequence failing on
2026-08-04 left anything half-applied; the entry above about the wedged `runserver` is the likely
reason migrations stopped running at all.

**The 18 are `0026`-`0043`, and running them is not a routine catch-up (chunk 360).** Most are
schema, but at least three carry data:

- `0027_places_backfill` - backfills the whole `Place` hierarchy;
- `0039_encrypt_contact_and_note_fields` - **encrypts existing column data**, and per
  `docs/DATA_ENCRYPTION.md` key handling here is unforgiving; running it against a database whose
  `UL_FIELD_ENCRYPTION_KEY` differs from the one in use when rows were written is how data is
  orphaned;
- `0042_label_merge_duplicates` - merges duplicate labels, i.e. deletes rows, immediately before
  `0043_label_unique_constraint` adds the constraint that requires it.

**Corrected 2026-08-14 (chunk 364) after reading the migrations rather than their names.** The
claim that `0042`/`0043` "cannot be half-run" was wrong: neither sets `atomic = False`, so on
Postgres Django wraps each in its own transaction - `0042` either fully applies or fully rolls back,
and a failure in `0043` leaves merged data without the constraint, which is retryable.

The real risk is **irreversibility, not partial application**. `0042`'s reverse is a documented
no-op:

> "Merging cannot be undone - the dropped rows are gone. Reversing the migration removes the
> constraint, which is enough to get the schema back; the merged data stays merged."

So `migrate` forward is safe to attempt, but there is no way back to the pre-merge label data via
`migrate` at all. **Take a database snapshot** - that advice stands, for this reason rather than the
one originally given.

**`0039` verified too (chunk 365), and here the original warning was right.** `_encrypt_column`
rewrites each value in place through `_field.get_prep_value()` - i.e. under whatever
`UL_FIELD_ENCRYPTION_KEY` is active *at migrate time* - across 9+ columns including
`dashboard_profiles.phone_number`, `.bio`, `.signal_username`, `.matrix_handle`, `.discord_username`,
`.area`, both Google account tables' emails, and `dashboard_safety_contact_defaults.email`. Its
`reverse_code` is `migrations.RunPython.noop`.

**So both data migrations in the pending batch are irreversible**, which is the single strongest
argument for the snapshot: `migrate` forward is attemptable, but neither `0039` nor `0042` can be
walked back, and between them they rewrite personal contact data and delete label rows.

Confirm the encryption key in the container's environment is the one you intend to keep *before*
running this - per `docs/DATA_ENCRYPTION.md`, changing it afterwards outside the documented rotation
procedure orphans every row this migration writes.

**Ordering matters, and the safe order is not the obvious one (chunk 361).** Celery workers do not
autoreload, so they are still running the code they started with on 2026-08-04 - which matches the
*old* schema, which is why no `ProgrammingError` has appeared since. The container's `/app/src` has
since been resynced with current code, so **the moment the stack is restarted the workers pick up
new code against the old database and the schema errors return**.

So: `migrate` (after snapshotting) **before** `docker compose restart`, not after. Restarting first
to fix the wedged `app` container will also break the workers, which are currently healthy and
processing their hourly tasks normally.

This range matches the container-drift note already in `CLAUDE.local.md` ("30 tracked files behind -
missing `models/place`, `models/album`, `models/map_overlay` ... and migrations 0026-0038", dated
2026-08-06), so the drift has been known for over a week in one form and unrecognised as a
*database* problem.

## Note 2026-08-14: `trip.py`'s masking docstring cites an entry that is not here

`controllers/trip.py:135` (`_mask_trip_identities`, or its equivalent) opens:

> `docs/PROBLEMS.md` gap: ``services/identity_visibility.py`` masked the single-trip render sites
> (member panel, activity/comment attribution) but not the trips list...

**There is no such entry.** The masking gaps recorded here cover the data export, global search, and
reply/reaction notifications (all 2026-08-07); the only trips-list entry is about *query
amplification*, which is unrelated. Searching for "trips list" or trip identity masking finds
nothing matching.

Two readings, and the difference matters to whoever picks this up: either the gap was closed by the
very function carrying the comment and its entry was removed without updating the reference, or it
was never filed and the docstring is the only record. The comment's phrasing ("masked ... but not
the trips list") reads as *describing a gap that still existed when written*, which favours the
second.

Recorded rather than resolved - deciding which requires the history behind that function, and the
answer changes whether this is a stale pointer or an unfiled gap.

## Note 2026-08-14: "remove `docs/notes/ai/` committed secrets" does not describe this repository

`docs/designs/rejected-and-deferred/split-architecture.md` (phase 8, Hardening) lists "remove
`docs/notes/ai/` committed secrets and rotate...". That line will alarm anyone who reads it, so:
**verified, and it does not apply to this repository's history.**

- `git log --all -- 'docs/notes/ai/*'` returns nothing - no file under that path has ever been
  committed on any branch.
- `git ls-files docs/notes/` shows only `mobile_app_notes.md` and `mobile_app_requirements.md`; the
  `ai/` subdirectory is ignored (`.gitignore:49`) and untracked.

So there are no committed secrets from that path here. The line is most likely written against the
*post-split* repository the document is proposing, or it is stale. Either way it currently reads as
an unaddressed security item in this repo and is not one.

Worth leaving the line alone until someone who knows the split plan can date it - but worth having
the verification recorded next to it, because the natural reaction to "committed secrets" is to start
rewriting history, and there is nothing here to rewrite.

## Reference 2026-08-14: where per-viewer visibility is enforced (six mechanisms, six places)

Not a defect - an inventory, recorded because audit chunk 394 established that this codebase
enforces visibility **per subsystem** rather than through one convention. That is a reasonable design
(each subsystem's notion of "who may see this" genuinely differs), but it means no single grep finds
them all, and a reviewer who learns one mechanism will not recognise the others.

| mechanism | where | guards |
|---|---|---|
| `visible()` queryset method | `models/device_scan/queryset.py` | device-scan markers |
| `viewer_hidden_activity_ids` | `services/trips/trip_visibility.py` | trip activity locations |
| `display_identity_for` | `services/messaging/direct_messages.py` | sender names in DMs/group chats |
| `*_for_viewer` helpers | `controllers/safety.py`, `services/trips/trip_access.py` | safety + trip per-viewer reads |
| masking helpers | `services/profile/identity_visibility.py` | profile identity across surfaces |
| place-domain access | `services/wiki/wiki_access.py` | wiki visibility by place domain |

**Adding a new surface that returns another user's data means picking the right one of these six**, and
the audit found at least one historical bug in each of the first four categories' problem space
(reply/reaction notifications naming masked people, the Google Calendar export leaking hidden
coordinates, trip location visibility re-implementing the shared gate more strictly, the data export
disclosing masked members). Those are the recurring shape: a *new* surface that did not consult the
gate its subsystem already had.

## Reference 2026-08-14: audit of all 26 code references to this file

Every source file citing `docs/PROBLEMS.md` was checked (audit chunks 370-405).

| outcome | files |
|---|---|
| resolve to an entry | 16 |
| **dangling** | **8** |
| unresolved (subject may be filed under another description) | 2 |

**Seven of the eight dangling references cite the same thing**: a decision dated 2026-07-23, or
`completed.md` by name. Both live in `docs/notes/ai/`, which is **gitignored** (`.gitignore:49`) and
was never committed. So this is one absent document referenced from eight places - not eight
independent omissions - and the fix is either to promote those decisions into a tracked file or to
stop citing an untracked one from tracked code.

The two unresolved are `services/spotguessr/__init__.py` and `services/trivia/__init__.py`,
describing an import-order failure that celery workers trigger and `manage.py check` does not. The
nearest entry (`PinViewSet.basename` / `get_default_basename`, root cause not found) shares the shape
but not obviously the subject.

**What makes a reference findable**, from the 16 that worked: the comment contains a *distinctive
searchable string* - a symbol (`MapController.resolve_place`), a flag (`strict=True`), a date, a
quoted entry title, or a concrete symptom (`{"ok": true}` and the field never changes). What fails is
describing the problem in general words ("the report", "option (a)", "the trips list"). The single
best example in the codebase is `services/messaging/direct_message_shares.py`, which quotes its
entry's title verbatim.

## Note 2026-08-14: `SECRET_KEY` falls back to a per-process random key with no environment guard

Found by audit chunk 441. **No hardcoded secrets exist** - all 11 secret-named settings read from the
environment, and `EMAIL_HOST_PASSWORD` defaults to `""` rather than a weak value. One line deserves
a second look:

```python
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()
```

A random fallback is the right instinct - far better than a checked-in default someone forgets to
override. But it is **generated per process**, and nothing branches on `ENVIRONMENT_NAME` (defined
eight lines below) to require the variable outside local development. Consequences if
`DJANGO_SECRET_KEY` is ever unset in a multi-process deployment:

- gunicorn workers, the Daphne ASGI process and each Celery worker get **different keys**, so a
  session or signed value created by one is invalid to the others - presenting as random logouts
  rather than a configuration error;
- `EncryptedTextField` derives from `SECRET_KEY` when `UL_FIELD_ENCRYPTION_KEY` is unset (see
  `docs/DATA_ENCRYPTION.md`), so encrypted columns written under a random key become unreadable on
  the next restart.

**Not observed - this is a hazard, not an incident.** Production presumably sets the variable, and
`.env` handling is in place. The cheap hardening is a startup check: if `ENVIRONMENT_NAME` is not
local/development and `DJANGO_SECRET_KEY` is unset, raise `ImproperlyConfigured` rather than
generating one. That converts a silent, confusing failure into a loud one at boot.

## Reference 2026-08-16: why two callers of one function legitimately catch different exceptions

Chunk 537 worked the rest of chunk 536's divergence list - the sweep that found the archived
safety-chat 500 by comparing what each caller of a function catches. Six more candidates read, **six
false positives**, and the reasons are different enough that the list is worth keeping: it is what
makes that sweep re-runnable without re-deriving the same judgements.

Divergence is *expected*, not suspicious. A caller catches less than its sibling when:

1. **It guards before the call instead of after it.** `controllers/visits.py` checks
   `visit_logging_allowed(...)` and answers 403 before ever calling `create_manual_visit`, so
   `VisitLoggingDisabledError` is unreachable - and its comment says exactly why the redundant
   service-side check stays ("403 rather than a confusing 400"). `controllers/userprofile.py` bounds
   the trust rating to the valid range before calling `set_trust`, routing anything else to
   `clear_trust` - out-of-range is that widget's "clear" signal, not an error.
2. **It constructs the payload itself, so the raising branch cannot be reached.** The HTML pin
   editor builds `edits` from a fixed key set, all within `EDITABLE_PIN_FIELDS`, and never passes
   `visited`, so neither of `apply_pin_edits`' two `PinEditError` branches can fire. The API accepts
   a client-supplied field set and must catch.
3. **The arguments make the branch impossible.** `views_messaging.py`'s self-leave endpoint calls
   `remove_group_member(group, profile, profile)`; the `GroupChatPermissionError` branch is about
   removing *other* members, so only the validation branch is reachable - and it catches the shared
   `ValueError` base anyway, which is broader than its sibling's two named types, not narrower.
4. **The catch is doing a different job.** `controllers/pin_suggestions.py` wraps
   `accept_pin_suggestion` in a bare `except Exception` *inside a loop*, to stop one bad suggestion
   aborting a bulk action. The API endpoint handles a single suggestion, where there is nothing to
   isolate it from. `accept_pin_suggestion` declares no `Raises:` at all.
5. **The scan crossed a function boundary.** `join_trip` raises nothing; the `Raises:` attributed to
   it belonged to `leave_trip`, the next function in the file, inside a `grep -A 40` window. The
   same artifact class recorded earlier in this audit.

Add the one from chunk 536 - a handler in a **base class**, one frame above the call, which an
intra-function scan cannot see - and that is six ways to be correct while looking divergent.

The sweep is still worth re-running; it found a real 500 on a safety path. But its yield is roughly
one in ten, and every one of the nine needs reading rather than triage by shape.

## Enforced 2026-08-16 (chunk 557): a view's handlers must accept every parameter its routes supply

Three chunks in a row produced the same shape, and the third made it a class worth sweeping rather
than a coincidence worth noting:

| route | handler | missing | found in |
| --- | --- | --- | --- |
| `saved_filters.new` | `SavedFilterEditView.post` | `filter_uuid` was *required*, only `edit/` supplies it | chunk 552 |
| `pin.link.to` | `PinRelinkView.get` | `location_slug` absent entirely | chunk 556 |
| `pin.link` | `PinRelinkView.post` | (the filed detach product decision, not a signature fault) | chunk 551 |

Django resolves handler arguments at **request** time, so the failure is a `TypeError` that only
appears when somebody requests the mismatched route - and both real instances were on routes no UI
path exercises, which is exactly why they survived.

Swept directly: for every view class wired to two or more routes, does each handler accept the union
of the parameters those routes can pass? **753 view classes, 48 of them multi-route, zero
mismatches.** The class is closed.

`test_view_signature_route_guard.py` keeps it closed, because adding a route to an existing view
re-opens it silently - nothing at import time or in review notices. **Verified to bind**: restoring
`PinRelinkView.get`'s pre-fix signature makes it report exactly that method and parameter.

Two details it inherits from earlier mistakes here: parameters accumulate down the resolver tree
(reading only leaf patterns is what made `test_route_query_scaling`'s second version blind to most
parameterised routes, per its own docstring), and a handler taking `**kwargs` is skipped because it
accepts anything by construction.

## Keyboard-invoked context menu may swallow the next activation (unverified)

`label-picker.ts`'s `isMouseContextMenu` decides whether to arm click-suppression after a
`contextmenu` event:

```ts
const pointerType = (event as PointerEvent).pointerType;
return pointerType ? pointerType === "mouse" : event.button === 2;
```

`contextmenu` is a `MouseEvent`, so `pointerType` is generally absent and the `button === 2`
fallback decides. That correctly distinguishes a mouse right-click (button 2, no suppression) from
a touch long-press (button 0, suppression armed). But a context menu invoked from the **keyboard**
- the Menu key, or Shift+F10 - also reports `button === 0`, so it would arm the suppression with no
follow-up click ever coming. The guard then stays set until some unrelated later click, which is
exactly the failure the function's own docstring describes: "swallowing keyboard (Enter/Space)
activations in the meantime".

Not fixed here because it cannot be confirmed without a real browser, and the plausible
discriminator (`event.detail === 0` for keyboard-invoked menus) is a behaviour I would be asserting
rather than observing. Worth ten minutes with DevTools on the Organize page label picker: press the
Menu key on a label chip, then try to activate any chip with Enter, and see whether the first
Enter is swallowed.

## The planning and handoff documents referenced across the docs do not exist

Three different paths are cited for "what is planned" and "what previous agents did", and none of
them is in the tree:

| Path | Cited by | Status |
| --- | --- | --- |
| `TODO.md` (repo root) | `docs/FEATURES.md:4`, `docs/NOTES.md:344,402`, `docs/ROADMAP.md:4,13,124`, `CLAUDE.local.md` | Existed - 416 lines - deleted in `3f12e875` ("Release v0.5.0b0") |
| `docs/prompts/completed.md`, `docs/prompts/todo.md` | `CLAUDE.local.md` | Never tracked in git |
| `docs/notes/ai/completed.md`, `docs/notes/ai/todo.md` | `docs/ROADMAP.md`, `docs/designs/place-consolidation.md` | **Gitignored, not missing** - see the earlier `completed.md` entry |

This is not cosmetic. `CLAUDE.md` and `CLAUDE.local.md` both instruct contributors (including agents)
to consult these before assuming something is unbuilt or unplanned.

**Corrected 2026-08-17 (chunk 607): the ticket ids are *not* unresolvable, and this entry originally
said they were.** The root `ROADMAP.md` - a separate document from `docs/ROADMAP.md`, and one I had
not opened when filing this - carries 251 `UL-` references, including UL-294, UL-70, UL-360 and
UL-277, each against a one-line description of the planned work. So a reader chasing "see `TODO.md`
UL-294" can find what UL-294 *is*; what they cannot find is the file the citation names, or whatever
additional context it held. That is a smaller problem than the one first written here, and the
difference matters to whoever decides what to do about it. `docs/ROADMAP.md`
says it was itself "generated 2026-07-18 from a full review of `TODO.md`" and tells readers to keep
that file updated alongside it. Anyone following those instructions finds nothing and cannot tell
whether the answer is "not planned" or "the document is missing".

`TODO.md`'s content is recoverable:

```bash
git show 3f12e875~1:TODO.md > TODO.md
```

Whether it *should* come back is the owner's call - it was removed in a release commit, which may
have been deliberate. But the current state is the worst of both: the file is gone and five separate
documents still treat it as live. Either restore it or update those references; the same choice
applies to the two agent-note directories, where the fix may simply be deleting instructions that
point at paths which never existed.

**Corrected 2026-08-17.** The `docs/notes/ai/` row above overstated the case, and an earlier entry
in this same file had already established why: `.gitignore:49` ignores that directory, so those
files are local-only agent notes rather than lost ones. That entry also states the structural
problem better than this one did - *tracked documentation referencing gitignored content* - which
applies to `docs/ROADMAP.md` and `docs/designs/place-consolidation.md` citing them, and is a
different defect from a file being deleted.

What remains specific to this entry, and is not covered there: root `TODO.md` **was** tracked, in
git, and was removed in the `3f12e875` release commit while five documents went on citing it as
live - including `docs/NOTES.md` quoting ticket ids inside it. `docs/prompts/` is a third path,
cited by `CLAUDE.local.md`, that matches neither pattern.

Not actioned here because recreating 416 lines of someone else's planning document, or editing
four documents' cross-references, is a decision about the project's own record rather than a defect
in its code.

## A group message can still be sent under a key version a removed member holds

Removing a member from an encrypted group correctly flags `needs_rotation` (the group-key GET
compares envelope holders against current members with `!=`, so removals and additions both trip
it), and that is now pinned by a test. But rotation is client-driven, and the send path only
validates `key_version >= 1` - it never compares against the group's current version.

So a sender whose client has not yet refreshed - an open tab, a client that missed the rotation
prompt - keeps encrypting under the version the removed member still holds an envelope for. The
group's own members are unaffected; the question is only whether the removed member can read
messages sent after their removal.

In-app they cannot: `GroupMessageQuerySet.visible_window` bounds each member to their membership
stint, so the ciphertext is not fetchable once `left_at` is set. The exposure needs the ciphertext
obtained some other way - captured traffic, a database copy, a compromised host - which is precisely
the threat model end-to-end encryption exists for, so it is not nothing.

**Why this is filed rather than fixed.** The obvious server-side fix is to reject a send whose
`key_version` is behind the current one while `needs_rotation` is set. Any member may rotate (not
just the creator) and the concurrent-rotation race is already handled, so that much is safe. What is
not safe is the failure mode: rotation requires *every* member to be enrolled, and returns 409 when
one is not. A single un-enrolled member would then block the whole group from sending, turning a
confidentiality gap into an availability outage. Trading one for the other is a product decision.

Options, roughly in increasing cost: have the send path warn/log when it accepts a stale version;
have clients re-check rotation state before send rather than on poll; or reject stale-version sends
only when the group is fully enrolled (so the 409 case cannot arise).

## A deleted message's preview survives in the recipient's notification list

Fixed in chunk 572: the *delayed email* and *delayed WhatsApp/SMS alert* for a direct message now
skip a message the app would show as a tombstone, so unsending inside the 120-second delay window
stops the out-of-band copy going out.

Not fixed, because it needs a schema decision: the **on-site notification** raised for the same
message keeps its preview text.

- `services/messaging/direct_messages` stores `message=preview` - up to 120 characters of the body.
- `services/messaging/group_chats` stores `message=f"{sender}: {preview}"`, likewise 120.

Neither is touched by `delete_message_for_everyone` / `delete_group_message`, so after the sender
unsends, the thread shows "Message deleted" while the notification row still quotes what was said.

There is no way to clean it up precisely today: `NotificationLog` has no reference to the message it
was raised for, and its `url` points at the *thread* (the conversation, or the group), not the
message. Matching rows heuristically on profile + type + url + timestamp would be fragile and would
sooner or later delete the wrong notification.

Options:

1. Add a nullable generic reference (or a `message_uuid`) to `NotificationLog`, and clear or redact
   matching rows when a message is deleted. Cleanest, costs a migration.
2. Render notification previews through the message at display time rather than storing them, so a
   tombstone applies everywhere at once. Cleanest conceptually, largest change - and the stored text
   currently doubles as the push/e-mail body.
3. Accept it, and say so in the UI: the notification was already delivered when the message was
   live, which is arguably the same as the recipient having read it.

Worth deciding rather than leaving implicit, because the app currently promises "Message deleted" in
one surface while quoting the message in another.

## An export whose cleanup fails to enqueue is never swept

`run_export` schedules `cleanup_export_artifacts_task` in a `finally`, so every path - success, a
failed user load, an exception mid-export - asks for cleanup. Worker loss is covered too: the Celery
settings deliberately reconcile `visibility_timeout` against the longest countdown this app
schedules, which is this one at 3600s, so an unacked cleanup is redelivered.

The uncovered path is the enqueue itself. `schedule_export_cleanup` uses `safely_enqueue_task`, and
when that returns None (broker unreachable, which the settings above are tuned to fail *fast* on) it
logs `"Unable to schedule cleanup for export directory %s"` and returns. Nothing else ever looks at
that directory, so a ZIP containing the user's entire account - pins, photos, messages, profile -
stays on disk indefinitely, and the only record is one warning line.

Low frequency, but the retention story is "it is deleted an hour later" and in this case it is not.
This codebase already uses periodic backstops for exactly this kind of single-mechanism dependency:
`sync_stripe_subscriptions` is described as a "safety net for missed Stripe webhook deliveries", and
`SafetyCheckinChatConsumer` revalidates every 60 seconds "as a backstop for a dropped
partner_access_revoked broadcast".

The matching fix would be a periodic sweep of export/import working directories older than their
TTL, which needs no per-job bookkeeping - the directory's own mtime is enough. Filed rather than
added because it means introducing a beat task, and how aggressively to reap those directories is
an operational choice.

## Should logging out wipe the cached E2EE keys? (product decision, filed 2026-08-17)

`frontend/ts/shared/e2ee-store.ts` caches decrypted E2EE material in IndexedDB - the identity private
key, and every unsealed conversation and group key - so day-to-day use never prompts for a password
or recovery key. Nothing clears it on logout. `clearProfileKeys` exists and does the right thing, but
its only caller is the key-reset flow.

**This is deliberate and documented**, which is why it is a question rather than a defect. The
module's own header says the cache is keyed by profile slug so two accounts sharing a browser cannot
read each other's rows "by accident", and states the boundary plainly: "same-origin storage is the
trust boundary either way - this is bookkeeping, not isolation."

The question is whether an explicit logout should be treated differently from a page close. Someone
logging out on a shared or borrowed machine would probably expect their decrypted message keys to go
with them; someone logging out and back in on their own laptop would probably not expect to re-enter
a recovery key. Both are defensible, and the tradeoff is a product call rather than an engineering
one, so it is recorded rather than decided.

If the answer is "yes", `clearProfileKeys(selfSlug)` on logout is the whole change. Note also that
its docstring offers "logout-everywhere / key reset" as its purpose while no logout-everywhere
feature exists anywhere in the codebase - worth correcting whichever way this is decided.

(Raised while tracing the messaging/E2EE surface. It was recorded in
`docs/reports/2026-08-11-codebase-audit.md` at the time and carried in that session's running list of
owner decisions, but never written here until now - which is what made it worth catching: a filed
item that lives only in a narrative report is not filed.)

## `.dockerignore` does not exclude `.env`, so deploy-host image builds bake the secrets in

Found 2026-08-17 reading the Dockerfile, which had not been examined. `.dockerignore` lists caches,
`node_modules`, `docs/`, virtualenvs and editor directories - but not `.env`. The Dockerfile then does
`COPY --chown=appuser:appuser . /app`, copying the whole build context.

`.env` is correctly gitignored (`.gitignore:53`) and is 3.3 KB of real secrets on this host:
`DJANGO_SECRET_KEY`, database credentials, `UL_FIELD_ENCRYPTION_KEY`, Stripe keys, OAuth client
secrets and API keys for the plugin fleet. Keeping it out of git and then copying it into the image
undoes most of the benefit.

**Scope, stated precisely, because it is narrower than it first looks.** Images published to
`ghcr.io` by `.github/workflows/publish.yml` are built from `actions/checkout`, which produces a
clean git checkout - `.env` is gitignored and therefore absent, so **published images do not contain
it**. The exposure is images built *on the deploy host*, which is exactly how `bin/deploy.sh` builds
them (`docker compose up --build`). There, anyone who can read the image can usually already read
`.env` on the same filesystem, so the practical gap is narrow: `docker save`/export of that image, or
Docker-daemon access without filesystem access.

**Not fixed here**, and the reason is worth recording because it is not squeamishness:
`settings/app.py` deliberately loads `Path(DEFAULT_ROOT, ".env")` - the Pydantic settings read that
file from disk, with a comment explaining where it lives relative to `src/`. `docker-compose.yml`
supplies configuration through `environment:` blocks rather than `env_file:`, so the baked copy is
*probably* redundant, but "probably" is not enough to justify a change that could alter how a
production container resolves its configuration, in an environment where I cannot run the real
deployment to find out.

**`.git` is a different matter and is *not* a defect.** It is also copied in, and that is deliberate:
`core/version.py` shells out to git to compare the deployed commit against upstream, and the
Dockerfile configures `git config --system safe.directory /app` for development images so that keeps
working. Excluding `.git` would break the version check. Noting it explicitly so nobody "fixes" it.

## `npm run git-squash` is a force-deploy with none of `deploy.sh`'s guards (minor)

Noted 2026-08-17 while confirming that `gunicorn.conf.py` is actually loaded. `package.json` defines:

```
"git-squash": "pkill gunicorn && git fetch origin && git reset --hard origin/main && npm run start"
```

Two things about it, neither urgent:

1. **It hard-resets to `origin/main` with no dirty-tree check.** `bin/deploy.sh` refuses to deploy
   when the working tree has uncommitted changes, and says so; this one discards them silently. Same
   repository, same operation, and the safety exists in one place only - the pattern already recorded
   for `deploy.sh` versus `clone_prod_to_staging.sh`.
2. **The `&&` chain aborts if gunicorn is not running.** `pkill` exits non-zero when nothing matched,
   so on a host where the server is already stopped the script fetches nothing, resets nothing and
   starts nothing. That direction fails safe, but silently, and the name gives no hint that it stops.

Also worth noting the name: it neither squashes nor touches git history - it is a force-redeploy. A
reader reaching for it expecting a history operation gets a hard reset and a server restart.

Not changed: it is a convenience script in `package.json`, its behaviour may be exactly what its
author wants at a terminal, and `bin/deploy.sh` already exists as the safe path. Recorded so the
difference between the two is a choice rather than a surprise.

### Pin suggestion `hit_count` is a read-modify-write (noted 2026-08-17)

`services/pins/pin_suggestions.py`'s `_upsert_matched_suggestion` and
`_upsert_new_pin_suggestion` do `existing.hit_count += _weight_of(...)` and save, and their caller
`ingest_location_hits` takes no lock. Two concurrent ingests for one profile - a repeated Immich
sweep overlapping a local-scan upload, which the function's own docstring names as the case it
handles - lose an increment, and can also both miss on the check-then-act and create duplicate
pending suggestions for the same pin.

Left unfixed deliberately: unlike the ledger/wiki/rating cases, `PinSuggestion` rows are per-profile,
so contention needs one user running two scans at once, and the damage is an undercounted ranking
signal rather than lost money, a reverted edit, or a discarded rating period. Worth an `F()`
expression plus a unique constraint if suggestion quality is ever reported as flaky.


### Fourteen documentation citations still point at the wrong line (noted 2026-08-17)

`bin/check_doc_line_refs.py --report-drift` lists them. They survived the 2026-08-17 sweep because
they can't be repaired mechanically, and they split into two kinds:

- **The line moved, but the anchor isn't a definition.** `settings/base.py:343` for
  `hard_delete_expired_direct_messages` (now line 374) is cited via its entry in the beat-schedule
  dict, and `tasks.py:1629` for `RUN_LOCK_CACHE_KEY` via an import. Renumbering these is safe but
  needs a human to confirm which usage was meant.
- **The symbol no longer exists at all.** `controllers/trip.py:135` cites `_mask_trip_identities`
  and `services/ai/anthropic.py:117` cites `send_prompt`/`send_prompt_list`; neither name appears
  anywhere in the tree now. The repair is rewriting the sentence around whatever replaced them, not
  changing the number - and guessing at that would put invented history into the record.

The eight that *were* mechanically provable (anchored on a `def`/`class` the tool could locate
uniquely) are fixed, and `check_doc_line_refs.py` now runs in CI to keep past-end-of-file citations
at zero.

## RESOLVED 2026-08-19: two encryption migrations disagreed about whether encrypting is reversible

0048 now carries a real decrypting `reverse_code` (`decrypt_existing_preference_fields`), modelled
on 0039's: same `gAAAA%` discriminator so plaintext rows are left alone, and the whole rollback
aborts if any token-shaped value cannot be decrypted under the configured keys.

What made this decidable rather than a standing argument: the policy had **already been written
down**. `docs/DATA_ENCRYPTION.md`'s "Migration rollbacks decrypt" (2026-08-15) says rollbacks
decrypt and abort rather than write garbage, and 0007 and 0039 implement it. 0048 landed two days
later reversing to noop, which left one file contradicting a documented rule - and it was the more
dangerous side of the pair, because noop makes `migrate dashboard 0047` *succeed* while leaving
ciphertext in columns pre-0048 code reads as plaintext. A failure someone can act on beats a success
that corrupts.

The exemption is removed from `tests/hypothesis/test_migration_noop_reverse_guard.py`, and
`test_migration_0039_reverse.py` now covers 0048 with the same three assertions it applies to 0039.

## Two tests fail only under a randomized full-suite run (2026-08-18)

The full suite on `810edd7b` reported three failures. One was real and is fixed
(`test_share_pin_copy_fidelity` - `Pin.buildings_auto_nested_at` was added without being
listed as copied-or-skipped, which is exactly what that guard exists to catch). The other two
pass in isolation and pass together, so they are order-dependent rather than broken:

- `test_safety_chat.py::SafetyCheckinChatConsumerTests::test_owner_and_contact_exchange_messages`
- `test_migration_0039_reverse.py::Migration0039ReverseTests::test_encrypt_decrypt_round_trips_and_ciphertext_is_discriminable`

Neither touches anything the floorplan/auto-nest work changed, and the same two passed in the
earlier clean full run on `90cf9c97`, so the trigger is whatever ordering `pytest-randomly` chose
that run - a consumer left connected, or key/settings state leaking from an earlier test, are the
two shapes worth looking at first. `-p no:randomly` hides it; reproducing needs the failing seed,
which this run did not record because `-q` suppressed the header.

Worth fixing properly rather than pinning the seed: an order-dependent test is a test that will
fail on someone else's machine for no visible reason. When picking it up, run the full suite with
`-p randomly --randomly-seed=<n>` and bisect with `pytest --randomly-seed=<n> -x`.

## Native `<select>` popup stays light-on-light in dark mode despite `color-scheme: dark` (2026-08-22)

The floorplan editor's opening-type `<select class="form-input">` (and likely every other bare
`<select>` on the site) is illegible in dark mode: unselected `<option>` rows render as light grey
text on a light grey popup background. `_dark.scss` already had `[data-theme="dark"] select {
color-scheme: dark; }` for exactly this (the standard fix - native option-list popups are
browser/OS chrome, not the page's own CSS box, and `color-scheme` is the documented hook for
telling the browser to use its dark native-widget palette there). Verified live that the rule
does land - `getComputedStyle(select).colorScheme` reports `"dark"`, and `data-theme="dark"` is
present on `<html>` - yet the popup still rendered light in both `--headless=old` and
`--headless=new` Chromium.

Added `color-scheme: dark;` directly on `[data-theme="dark"]` itself too (not just nested under
`select`), on the chance an engine reads the document root's scheme rather than each control's for
this - still no visible change under either headless mode.

This didn't budge across two different Chromium rendering paths with the CSS verified correct, so
the remaining suspect is Linux/GTK-specific: Chromium's `<select>` popup on Linux is known to
sometimes defer to the system GTK theme for the dropdown list chrome rather than the page's
`color-scheme`, independent of what the page declares. If that's what's happening, no page-level
CSS can fix it - the actual fix would be replacing the native `<select>` with a custom-styled
dropdown (`<ul>`/`<div>`-based combobox), which is a real UI component to build, not a styling
tweak, and wasn't attempted here since it's out of scope for what looked like a small dark-mode
fix.

Left the `color-scheme: dark` additions in place (correct regardless, and may well fix this on
Windows/macOS Chrome, which are more likely to honor it than Linux Chromium's GTK-backed popup) -
someone should verify on a non-Linux browser whether this is actually resolved there before
deciding whether the custom-dropdown rewrite is worth doing.

## No working headless-browser path exists on this host for live UI verification (2026-08-22)

Tried to drive the floorplan editor in a real browser (per the standing rule that a UX/behavior
claim needs to be run, not just read) against a fresh `bin/dev_env.py` environment. Every cached
Playwright Chromium build on this host - `chromium-1148`, `chromium-1228`, and both matching
`chromium_headless_shell` revisions - fails to launch with the same error:
`error while loading shared libraries: libatk-1.0.so.0: cannot open shared object file`. This is a
missing system GTK dependency, not a Playwright/browser-revision mismatch (confirmed across three
different cached revisions and both the full-chromium and headless-shell binaries). No passwordless
sudo exists on this host to `apt-get install` the missing libraries (see `CLAUDE.local.md`), so this
could not be worked around in-session.

Practical effect: nothing in this environment can currently drive a real browser end-to-end
(screenshot, click, read computed styles). Static code reading plus backend-level reproduction
(a real Django test client hitting the actual view/serialization code) is the fallback, and is what
this session used instead - see `test_floorplans.py`'s `FloorplanSessionItemIdentityTests` for an
example of proving a frontend/backend interaction bug this way without a browser. That fallback
cannot catch anything that only manifests in rendered layout, computed CSS, or real pointer/drag
event sequences (exactly the class of bug the SCSS/dark-mode entries above needed a browser for).

Whoever next needs actual browser automation here should either get the missing GTK libraries
installed (a one-time host fix, needs sudo) or use `/run-skill-generator` to capture whatever
does end up working as a committed project skill, so the next session doesn't rediscover this.

## RESOLVED 2026-08-22: `pyproject.toml`'s mutmut test-selection pointed at the wrong file for `identity_visibility.py`

`bin/run_mutation_tests.sh --results` showed all ~105 mutants in
`services/profile/identity_visibility.py` as `no tests` rather than `killed`/`survived` - a
different, meaningless status: mutmut never even ran a test against them. The configured
`pytest_add_cli_args_test_selection` listed `test_identity_visibility_batch.py` for this module,
but that file tests `Profile.visible_profile_pks`/`Friendship` batching and never imports
`services.profile.identity_visibility` at all - confirmed by grepping its imports. The actual
tests (`test_identity_visibility.py`, plus `test_dm_share_live_updates.py`,
`test_map_pin_share_detection_integration.py`, and `test_query_scaling_group_members.py`, all of
which directly import `resolve_visible_identity(ies)`) were simply missing from the list.

Fixed by adding those four files to the selection (and, while already in that section of
`pyproject.toml`, two files that were the same kind of miss for `wiki_edits.py`:
`test_wiki_revert_of_revert.py` and the new `test_wiki_boundary_revert.py`, both of which
directly exercise that module but weren't listed either - only `test_wiki_edit_field_scope.py`
was). All seven newly-added files pass together as a baseline (124 tests, 98.82s).

**Not independently re-verified against a fresh mutation run** (a full pass across all three
`only_mutate` modules is expensive - see `bin/run_mutation_tests.sh`'s own "roughly one mutant per
second" estimate, likely much longer with hundreds of Django tests per mutant); the fix is
grounded in confirmed import analysis, not an empirical before/after kill-rate comparison. If a
future mutation run still shows `identity_visibility.py` mutants surviving now that real tests are
selected, that would be genuine, actionable coverage gaps - worth a closer look then.

## RESOLVED 2026-08-22: `PinList`'s edit endpoints reverted concurrent edits to untouched fields

Both `PinListEditView.post` (internal, HTMX-driven inline rename/description edit and smart-rule
changes) and `PinListDetailView.patch` (external API) ended with a bare `pin_list.save()` -
writing every column from that request's in-memory snapshot. A rename request that loaded the row
before a concurrent request (another tab, or the other of these two independent
implementations) changed `description`/`smart_filter`/`smart_boundary`/etc. would silently revert
that other change the moment its own save ran, last-write-wins on the whole row rather than just
the fields it actually touched. Same bug class as the one `services/wiki/wiki_edits.py`'s
`save_edited_fields` already guards against on a comparably shared model.

Fixed both views the same way: track a `changed_fields: set[str]` as each field is actually
mutated, then `pin_list.save(update_fields=[*changed_fields, "updated"])` instead of a bare
`save()` (skipped entirely when nothing changed). Confirmed the `PinListsCollectionTests.post`
create path is not a lost-update risk - `pin_list = PinList(...)` there is a brand-new unsaved
instance, so a bare `save()` on it is a normal create, not this bug.

Proved with a new regression test per endpoint (`PinListEditConcurrentWriteTests` in
`test_pin_lists.py`, `PinListDetailTests.test_concurrent_edit_to_another_field_survives_a_rename`
in `test_external_api_lists.py`): each patches the view's own list-loading function with a
`side_effect` that injects a concurrent `PinList.objects.filter(pk=...).update(description=...)`
between that function returning and the view's `save()` - simulating the actual race rather than
a pre-request update the view's own read would already see. Both tests fail against the old bare
`save()` (confirmed via `git stash` on just the source files) and pass with the fix; full
`test_pin_lists.py` + `test_external_api_lists.py` suite (97 tests) passes.

Not yet investigated: the same bare-`save()` lost-update shape may exist on `Label`, `Trip`,
`TripActivity`, `SavedFilter`, `Place`, and `Album`'s own edit endpoints - flagged as candidates
by a fix-density read of `report_model_writers.py`'s output, not yet individually verified as
real bugs (the `PinList` create-path false positive above shows not every bare `save()` is one).

## `SavedFilterDetailView.patch` has a pre-existing mypy type error (found 2026-08-22, not fixed)

`external_api/views.py`, `SavedFilterDetailView.patch`: `saved_filter.color = clean_color(data["color"], default="...")` -
mypy reports `Incompatible types in assignment (expression has type "str | None", variable has type
"str | int | Combinable")`. Found incidentally while mypy-checking an unrelated `PinList` fix in the
same file; not investigated or fixed - out of scope for that change. `SavedFilter.color`'s
Django-stubs-inferred field type looks like the actual mismatch (an F-expression-shaped type rather
than the plain `str` the field should behave as); worth a look next time this file is touched.

## A pool reference with no image cannot keep its identity (2026-08-23)

**RESOLVED the same day.** `applyServerIds` matched a reference-pool row to the
server's by the image it stood for, so a row with no image - a reference added by
URL, or a `source_pool` row - had no key, kept its client-side uuid, and was
destroyed and recreated on every save.

The note here said the obvious fix did not work, and it did not: `FloorplanSource`
and `FloorplanReference` extend `FrontendDashboardModel` rather than
`FloorplanItem`, so they had no `sort_order`, and `ordering = ("id",)` would have
made the order deterministic without making it match the payload - new rows are
created after the existing ones.

So they have a `sort_order` now, written from the payload index the way `_sync`
already does for every other collection (0064_floorplan_pool_sort_order), and
both models order by it. The pool comes back in the order it went out, the client
matches positionally like everything else, and the image special case is gone.

## Five mypy errors outside the floorplan work (2026-08-23)

`mypy src/urbanlens` reports five errors in four files, none of them in code
this floorplan pass touched:

- `conftest.py:50` — an unused `type: ignore` *and*, on the same line, a real
  `setdefault` argument mismatch. The ignore is presumably why the mismatch went
  unnoticed.
- `services/core/text_limits.py:52` — a `"type"` attribute error
- `plugins/builtin/property_records.py:201`
- `external_api/views.py:2119` — `clean_color(...)` returns `str | None` into a
  field typed `str | int | Combinable`

Left alone deliberately: `external_api/views.py` has uncommitted changes from
other work, and the rest are unrelated to this pass. Recorded here rather than
fixed in passing, because a change to a file someone else is mid-edit on is how
two people's work collides.

The one that *was* in scope is fixed: `controllers/floorplans.py` narrowed a
geometry with `polygon.geom_type == "MultiPolygon"`, a string comparison neither
mypy nor a reader can use, before iterating it - and a `Polygon` is not
iterable. It is an `isinstance` check now.

## Every drag frame rebuilds every Leaflet layer (2026-08-23)

`render()` in floorplan-editor.ts clears all four layer groups and recreates
every polyline, polygon, marker and handle, and a drag calls it synchronously on
each pointermove. Measured in the browser on a 12x12 grid of rooms - 312 walls,
smaller than a real survey - that cost **229ms per move**, about four frames a
second.

Most of it was the room labels: a label is a *permanent* Leaflet tooltip, so
every one is a DOM node Leaflet creates and positions itself. Suppressing them
while `activeDrags` is non-zero, the way the joint handles already were, brought
it to **49ms** and they return on release.

What is left is the rebuild itself: ~312 `L.polyline` plus ~144 `L.polygon`
constructions per frame, against ~7ms for `deriveFaces` on the same plan (so the
planar geometry is not the problem). The fix is to reuse layers - `setLatLngs`
on the ones that already exist, create and destroy only what actually appeared
or went away - rather than clearing and rebuilding. That is a real refactor of
`render()` and its callers, not a patch, which is why it is written down here
instead of attempted alongside the measurement.

`bun run test:browser` holds both guards: the behavioural one (no room labels
mid-drag, labels back on release) and a deliberately loose timing bound.

**Priority, measured rather than assumed.** Editor cost per move, with the
harness's own per-move overhead subtracted:

| grid  | walls | rooms | ms/move | fps |
|-------|-------|-------|---------|-----|
| 4x4   | 40    | 16    | ~13     | 77  |
| 6x6   | 84    | 36    | ~18     | 55  |
| 12x12 | 312   | 144   | ~33     | 30  |

Linear in plan size, and a real floor is 20-40 rooms, so the refactor buys most
of its value on plans larger than anyone is likely to draw. Worth doing, not
worth doing ahead of anything that is actually wrong.

## The building shell is a room, but a room bounded only by shell cannot be moved (2026-08-22)

Jess reported two things that turn out to be the same case, pulling in opposite
directions: "the base floorplan is considered a room in some cases", and "when a
room shares walls with the exterior floorplan, it is sometimes not considered a
room for some things (like being deleted, moved, etc)."

What the code did: `roomBoundaryWalls` treated a wall as the room's own only
when it bounded no other face *and* was not exterior.

The claim originally written here - "a corner room in a subdivided building
still has its partitions, so it moves and deletes normally, that half already
worked" - was **wrong**, and checking it rather than repeating it is what turned
it up. In a planar subdivision *every* interior partition borders two faces, so
"bounds no other face" is never true of one. A closet inside a building owned
nothing at all, and the move, the turn and the delete each declined on the
grounds that there was nothing to act on. Worse than declining, in the case of
the drag: `start` returning false hands the gesture to Leaflet, so trying to
move a room panned the map instead.

The exterior exclusion cannot simply be dropped. Verified against planar.ts: in
an 8x4 shell split by one partition, `west` bounds only the west face, so a
purely topological "unique" would translate the building's west wall whenever
that room is dragged. That was tried and reverted.

**RESOLVED 2026-08-23.** Jess left the call here ("low risk to choose an
implementation that makes sense and is consistent with other apps; I can give
feedback after testing"), so:

Nothing in the geometry separates a shed from a shell nobody has subdivided,
and no rule was going to invent the difference - so *intent* draws the line,
which is what other floorplan tools do too. An un-subdivided outline is not
captioned as a room and a stray click will not mint one; right-clicking it
offers to name it, and once named it is a room like any other. A studio flat and
a shed are single rooms; a building you have not got round to dividing is not.

"Like any other" now includes moving and deleting it, which it did not.
`splitRoomBoundary` (shared/floorplan/rooms.ts) hands a room the partitions on
its boundary regardless of what else they border, plus - for a face that is
entirely exterior - the shell itself, since a closed structure's walls bound it
and nothing else.

What it never hands over is the shell of a building the room merely sits inside.
That is the reverted bug's territory: topologically the west wall of a shell
split by one partition bounds only the west room, and letting the room have it
tears the side off the building.

So a room's own walls move and its neighbours' reshape, and the shell stays put
- but a partition that met the shell has to stay met, or moving a room would
leave it unenclosed. Corners resting on a wall the room does not own slide along
it instead of dragging it (`cornerAnchors`/`anchoredMove`), for the turn as well
as the move.

## Floorplan drags converted to Pointer Events without browser verification (2026-08-22)

All five drags in the floorplan editor (wall body, room fill, opening slide,
opening end, wall corner) moved from Leaflet mouse events to Pointer Events
bound on each layer's own element, so that they work with a finger. Before
this, a touch drag emitted no mouse events at all and only the Leaflet-native
marker drag worked on a phone.

**RESOLVED 2026-08-23: there is now a browser, and the conversion is verified.**
`apt-get download` plus `dpkg-deb -x` needs no root, so the missing GTK/ATK/ALSA
libraries are unpacked under ~/browserlibs and reached via LD_LIBRARY_PATH - see
`bin/browser_libs.sh`. Playwright is a dev dependency, and
`bun run test:browser` drives the real built bundle through real gestures,
including a synthetic touch drag.

That immediately paid for itself: it caught a regression the conversion had
introduced. Pointer capture retargets `pointerup` to whatever holds it, and the
browser fires `click` at the common ancestor of press and release - so capturing
on the press moved the click off the wall, and **clicking a wall to select it
had stopped working entirely** for mouse and touch alike. Capture, disabling the
map's own dragging, and stopping propagation now all wait until the gesture has
travelled far enough to be a drag rather than a click.

One defect was caught by review before it could ship further: the first version
bound the move/up phase to the *layer's* element. render() clears every layer
and runs on every frame of a drag, so the drag's own first move destroyed the
element it was bound to - releasing pointer capture and ending the gesture after
one frame. Tracking now happens on window, with capture on the map container,
both of which outlive render(). That is also why the old code bound to the map
and paid for it with the listener leak.

What to exercise first in a real browser, desktop and phone:
  - dragging a wall body, and the Ctrl (network) and Alt (detach) variants
  - dragging a room fill, and confirming a press on an *unselected* room still
    pans the map rather than moving it
  - dragging a wall corner and a door end - these previously had no slop, so
    check that a tap selects without nudging
  - that a plain tap still selects (the pointerdown handler deliberately calls
    stopPropagation and never preventDefault, precisely so the click survives)
  - two-finger pan and pinch-zoom over drawn geometry, which `touch-action:
    none` on .floorplan-wall/.floorplan-room/.floorplan-opening/.floorplan-handle
    could plausibly interfere with

Known gap left in place: the wall tool's rubber-band preview still follows
map.on("mousemove"), so on touch there is no live preview line while drawing -
taps still place corners, because Leaflet synthesises click from a tap.

## Autosave rewrites every row, and the obvious fix loses data (2026-08-23)

`save_document` replaces the whole document, so every wall, opening, room seed
and marker row is written on every autosave tick - which fires on a debounce
after each edit. A four-wall plan with one room and one marker costs 33 queries
for a save that changes nothing (`FloorplanAutosaveCostTests` pins that as a
ceiling); a 400-wall plan costs proportionally more. Each marker's twin `Pin`
and its `Location` are also re-resolved and re-saved every time, firing the full
Pin signal chain.

**An attempt at the obvious fix was reverted, and anyone retrying it should read
this first.** Skipping `row.save()` when a row's stored values are unchanged -
snapshotting the concrete fields before the builder touches the row, comparing
after - measurably works (33 -> 27 queries) and **silently destroys
`FloorplanLock` rows when an opening moves between walls**
(`FloorplanOpeningRehostTests::test_a_moved_opening_keeps_its_locks` fails).

What was ruled out while chasing it, so it need not be re-done:

- The lock sync itself is fine: it was instrumented, and receives
  `existing=[<uuid>] payload=[<same uuid>]`, so the lock is matched and kept.
- The orphan-opening sweep is fine: `existing == surviving`, nothing deleted.
- Bisected to the single line - restoring an unconditional `row.save()` makes
  the test pass with every other part of the change still in place.

So the mechanism is somewhere between "the opening's FK change is not persisted"
and the cascade from `FloorplanOpening` to `FloorplanLock`, and it was not worth
shipping a data-loss risk to find out. If this is picked up again, start by
asserting the opening's `wall_id` actually reaches the database, and treat any
version of it as unshippable until that rehost test passes.

The related idea - skipping the twin `Pin` save when nothing changed - was
reverted with it, untested on its own. It is probably safe and should be
attempted separately, with `FloorplanMarkerLinkedPinTests` as the guard.

## Three third-party CDNs are single points of failure (2026-08-23)

toastr (cdnjs), Leaflet and leaflet-rotate (unpkg) are `<script>` tags, not
bundled. Each is absent whenever that request does not land: offline, a blocked
CDN, a corporate proxy, an SRI mismatch, or a CDN outage. This is not a rare
state for a field app whose users are on bad mobile data by definition.

The immediate damage is fixed. `shared/dialogs.ts`'s `toast` no longer throws
when toastr is missing (it falls back to toastr's own markup, which
`sass/_toastr.scss` already styles, since only the script is remote), and the
floorplan editor now says the map did not load instead of leaving a blank
rectangle. Both are regression-tested. The *class* of problem is not fixed:

- Anything reached only through a CDN global still needs its own guard, and
  nothing enforces that. `window.toastr` is now typed optional, so TypeScript
  catches new unguarded uses of that one - Leaflet's `declare const L` is not,
  and cannot easily be, since the editor is built on it top to bottom.
- Templates are not typechecked at all. `themes/base.html` had eight unguarded
  `toastr.*` calls; the one at the top of its main inline `<script>`
  (`toastr.options = {...}`) threw before the `showToast` and
  `htmx:responseError` listeners below it were ever registered - so those
  listeners' own careful `if (window.toastr)` guards were dead code. All eight
  are guarded now, but a ninth can be added tomorrow with nothing to object.
- Guarding degrades gracefully; it does not restore the feature. A blocked
  unpkg still means no map.

Self-hosting all three would remove the class rather than the instances, and is
the actual fix. It is not free: Leaflet is loaded by several pages, not just the
floorplan editor, and `leaflet-rotate` patches `L` in place and must keep
loading after it.

Worth knowing while that stands: **`bun run test:browser` fetches Leaflet from
unpkg on every run**, so the browser suite needs working outbound network to a
third party and fails offline for reasons unrelated to the code under test.

## docker-compose.hot-reload.yml crash-loops when the checkout is not the container's uid (2026-08-23)

Bringing an agent dev environment up with the hot-reload overlay puts `app` into
a restart loop and the site answers 502:

```
File "/app/src/bin/init.py", line 335, in build_frontend
    frontend_dir.mkdir(parents=True, exist_ok=True)
PermissionError: [Errno 13] Permission denied:
    '/app/src/urbanlens/dashboard/frontend/static/dashboard/css'
```

`docker cp` preserves source ownership and a bind mount exposes it directly, so
the container's `appuser` cannot write anywhere inside the mounted tree. The
overlay already knows this - it redirects `UL_LOG_DIR` out of the tree for
exactly this reason, and its header explains why - but `init.py`'s
`build_frontend()` also writes into the mounted tree on every start, and that
was not accounted for. It only shows up where the checkout belongs to a
different uid than the image's `appuser`, which is every environment
`dev_env.py` creates.

The overlay delegates SCSS to a `sass-watch` sidecar already, so the app
container's own frontend build is redundant under hot reload. Teaching `init.py`
to skip it (an env var the overlay sets) looks like the fix, rather than
loosening permissions on the checkout.

Until then, updating a `dev_env.py` environment means the documented
`docker cp` + `chown` route rather than hot reload - and note that
`STATIC_ROOT` is a *separate* collected tree (`src/urbanlens/frontend/static`,
not `dashboard/frontend/static`) served by whitenoise even with `DEBUG=True`,
so a copied-in JS bundle is not served until `collectstatic` runs.
