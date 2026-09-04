# Resolved problems (archive)

Entries from `docs/PROBLEMS.md` whose headers record them as resolved, fixed or dismissed,
moved here on 2026-08-18 so the live file lists what still needs attention.

Kept rather than deleted: several of these are the only written record of *why* something is
shaped the way it is, and a few document traps that would otherwise be rediscovered the hard
way. Search here before concluding a defect is new.

Note for anything citing this material by line number: `docs/reports/` contains audit reports that
quote `PROBLEMS.md:<line>`. Those numbers refer to the pre-split file and now point at different
content - follow them by *searching for the quoted text*, not by jumping to the line.

## RESOLVED 2026-08-12: a password reset does not evict an intruder who minted an API key

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

**Fixed 2026-09-04**, with the owner's ruling: ask via a dialog, default to *not* revoking, and do not ask at all when the account has no active keys.

Asked on the reset POST, because `post_reset_login` is False - that request is the only moment in the flow where the account is identified, so a prompt on the next page would have no principal to act as.

The dialog is backed by a hidden field rather than driven by one. No JavaScript, or JavaScript that throws, submits the form exactly as before and revokes nothing - the safe answer is the one that survives failure. The submit listener is on the form rather than on `document` (a capturing document-level handler would silence every other submit handler on the page) and is registered before `wireResetConfirmForm`, so the E2EE credential derivation waits for the choice and then replays via `requestSubmit()`.

**One thing to know before touching this page again**: the key count is gated on `validlink`, not on `self.user`. Django resolves `self.user` from the uidb64 *before* checking the token, and a uidb64 is an encoded integer pk - so anything rendered from `self.user` alone is readable for any account by anyone who can count. Confirmed load-bearing: weakening the guard to `user is not None` fails `test_an_expired_link_for_a_real_account_reveals_nothing`. Key *names* are never rendered at all; the count is enough to phrase the question, and names are user-authored text.

**The second half of this entry is still open**: `ApiKeyCreateView` still mints a key behind `LoginRequiredMixin` alone, with no current-password proof. That is what makes a session-only compromise enough to create the credential in the first place, and it is a separate decision (re-auth on mint, or notify-on-mint) rather than part of this one.

## RESOLVED 2026-08-21: Consensus points are awarded for reverting someone else's edit, and never retracted

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

**Fixed 2026-09-04**, with the owner's ruling: reward positive contribution, make farming unattractive, accept that it will not be perfect, and leave a note to reassess.

Three changes. A revert earns nothing (`WikiEdit.is_revert`, set at creation rather than derived from the `reverted_by` back-reference, because the award happens in the reverting row's own `post_save` - before the target row points at it). Reverting retracts what the reverted edit paid, and reverting the revert puts it back; compare-and-swap on a flag stored on the row, the shape `ReputationEvent.retracted` already uses, because several paths reach it for one edit - the revert, an admin toggling `reverted` on the change form, deleting an already-reverted edit - and only the first may move the total. And substantive fields are now worth more than an alias or a link, with one edit capped.

The award is recorded on the row rather than recomputed, because the weights are a first cut expected to be retuned and a retraction has to return exactly what was paid.

**Known and accepted**, noted in `points_for_changes`: reverting drains the reverted author's score, so a bad actor can aim it at a good contributor. Reverts are themselves revertible and restoration is wired, so it is recoverable rather than permanent - but nothing rate-limits it.

Worth recording separately, because it invalidated the guard this entry relied on: the AST scan in `test_friend_accepted_source_profile.py` was found to be matching nothing at all in the same pass (see its own archived entry). The completeness test here counts what the walk returned rather than a parallel string search, for the same reason.

## RESOLVED 2026-09-01: a source-scan regression guard is one reference short of its own threshold

`test_friend_accepted_source_profile.py::EveryFriendAcceptedSiteSetsSourceProfileTests.
test_the_scan_still_finds_the_sites` counts occurrences of the literal string
`NotificationType.FRIEND_ACCEPTED` across `services/social/friendship.py` and
`controllers/friendship.py`, asserting at least 3 ("Guard against the check passing because it
matched nothing"). It currently finds 2 - both in `friendship.py`; `controllers/friendship.py`
has none. `git log` on both files shows nothing recent enough to explain it (the last commit
touching either predates this branch), so this is not a regression from anything here - either a
third call site was consolidated away at some point without the test's threshold being lowered
to match, or one is missing and has been for a while. Not investigated further: distinguishing
"stale threshold" from "a friend-accepted notification stopped firing somewhere it used to"
needs more history on the notification call sites than a source scan alone gives.

**Fixed 2026-09-03**, and the answer was "stale threshold" - plus a second fault underneath it that this entry did not suspect.

The third site was consolidated away: `FriendController.friend_request_respond` stopped building its own notification on 2026-08-29 (`1899a8e64`) and now calls `accept_friend_request`. The HTMX path still raises the notification; it just no longer has its own copy of the code that can be wrong. Nothing stopped firing.

Underneath, the AST walk the threshold was guarding found **zero** sites, not two: it matched `.create(` and every site had moved to `.notify(`, the sanctioned entry point that applies the recipient's mute preferences. So `test_no_site_omits_it` had been asserting that an empty list is empty ever since. That is exactly the failure the threshold existed to catch, and it did not, because it counted a string in the file rather than what the walk returned. It now counts the walk's own output.

## RESOLVED 2026-08-31: opening the lightbox mid-scroll can inject a broken skeleton-placeholder item

Found reviewing Batch 5 (Vault Documents) - pre-existing in Vault Photos since Batch 2, unrelated to
Batch 5, which faithfully reproduces the identical pattern rather than introducing a new instance.
`window.photosOpenLightbox`/`window.documentsOpenLightbox` (pages/vault/photos.html,
pages/vault/documents.html) build the lightbox's item list from every `.photo-tile`/`.document-tile`
currently in the DOM, with no guard against an in-flight skeleton placeholder - `renderVaultSkeletonTile`/
`renderVaultDocumentSkeletonTile` (shared/vault-photo-grid.ts, shared/vault-document-grid.ts) give a
skeleton tile the *same* base class (`photo-tile photo-tile--skeleton` / `document-tile
document-tile--skeleton`) as a real one, distinguished only by the modifier class and the absence of
`data-id`. Opening the lightbox while a page fetch is in flight (a plausible click during
infinite-scroll) includes the skeleton(s) in the built item array with `imageId: NaN` (from
`parseInt(undefined, 10)`) and empty url/caption, producing a broken, empty lightbox entry reachable
via prev/next navigation. Worth filtering the tile query to `:not(.photo-tile--skeleton)` (and the
document equivalent) in both open functions.

**Already fixed when this was filed, and now guarded.** `89860a839` (2026-08-31, the day after this entry) added `[data-id]` to both tile selectors, which is exactly the filter this entry asked for; the entry was never updated.

What was still missing is anything holding it in place. The skeleton tile shares its base class with a real one and differs only by the modifier and by carrying no `data-id`; the selector lives in an inline `<script>` that `bun run typecheck` and the TS suite cannot see; the skeleton class lives in TypeScript. `vault-grid.contract.test.ts` now ties the two together, for photos and documents both.

## RESOLVED 2026-08-31: album detail resolves photo visibility four times per request

Found in a fresh-eyes performance review of the Vault feature; **pre-existing in the shared album
code** (`controllers/albums.py`), not introduced by the Vault, but the Vault made it reachable with a
much wider scope (a vault album's owner is a `Profile`, so its candidate set is the user's entire
library rather than one pin's photos). Measured with `CaptureQueriesContext` against the dev DB: a
30-photo vault album costs **60 queries**.

`_album_detail_context` (`controllers/albums.py:335-354`) resolves the same visibility set four
separate times - `visible_album_item_pairs` at :335, then again inside `album_images_page` at :337,
then `eligible_images_for` at :342, then `_picker_album_payload` -> `albums_listing` ->
`_visible_image_ids` at :354. `ImageQuerySet.visible_to` is documented as eager
(`models/images/queryset.py:100-106`) and costs ~9 queries a time (two friendship queries, pinned
locations, trip ids, the uploader/visibility scan, and `visible_wiki_location_ids`' three
place-domain queries). Roughly 36 of the 60 are the same work repeated. `album_images_page` should
take the already-computed pairs, and `_picker_album_payload` should reuse the listing.

Two related unbounded reads in the same function:

- `:342` `row["available_images"] = list(eligible_images_for(owner, viewer).exclude(...).only(...))`
  has no limit. `.only()` trims columns, not rows. `_album_detail.html:174` renders one `<li>`+`<img>`
  per row into the add-to-album dialog, so a user with 5000 photos ships ~5000 tiles of HTML every
  time they open any vault album. Needs the offset/limit treatment the album grid itself already has.
- `:350` `map_images` loads **every** visible photo in the album (not the current page) with a
  `Location` join and serializes them all via `json_script`, immediately after `album_images_page`
  deliberately avoided hydrating them.

And in `services/photos/albums.py:266`, `albums_listing` materializes every `AlbumItem` plus every
joined `Image` only to derive a count, a cover id, and a min/max timestamp (lines 276-283) - all
expressible as one `values("album_id").annotate(...)`. Line 285 then re-fetches covers that
`select_related("image")` had already loaded.

Left alone deliberately: these are all in long-standing shared album code that pin and wiki albums
depend on, and the safe fix is a focused refactor with its own test pass rather than a drive-by edit
during a Vault review.

**Fixed 2026-09-03.** The four viewer-scoped lookups behind `visible_to` (friends, pinned locations, trip memberships, reachable wikis) are memoised on the `Profile` instance, the idiom `wiki_access.visible_wiki_location_ids_cached` already used - and `_shared_within_reach_of` now calls that cached variant, which was the fourth repeated lookup. Every caller of `visible_to` benefits, not just album detail.

The bound on staleness is the instance: a `Profile` is loaded fresh per request, so an entry cannot outlive the request that made it and nothing has to invalidate it when a friendship or pin changes. `test_image_visibility_memo.py` pins both halves, since the failure mode of getting it wrong is silently widening or narrowing who can see a photo.

Worth knowing for any future query-count test in this area: two album tests measured constancy by comparing two albums against eight, and the first measurement now warms what the second reuses. They warm the memo before measuring; without that they compare a cold call to a warm one.

## RESOLVED 2026-08-31: `dashboard_images` has no index supporting the Vault's hot query

Found in the same review, verified against the live dev database (`pg_indexes` on `dashboard_images`,
18 rows): **no index contains `created`**, and none pairs `profile_id` with `media_type`. The only
declared indexes (`models/images/model.py:552-560`) are `(location, media_source_key, media_item_key)`
and `(profile, quota_exempt_reason)`.

Every Vault gallery page runs `WHERE profile_id = X AND media_type = 'photo' ORDER BY created DESC,
id DESC LIMIT 24 OFFSET n` (`models/images/queryset.py:269/275`, ordering from `models/images/sort.py:61-64`).
With only `dashboard_images_profile_id_c6ff6357` usable, Postgres reads every row the profile owns,
filters `media_type` on the heap, and sorts the whole set to return 24. That repeats per scroll page,
and again for the `.count()` each page fetch issues (`controllers/vault_photos.py:292-294`,
`controllers/vault_documents.py:110-112` - `total` is only needed once, on the first page).

The attention queue has the same gap: `needs_attention` (`queryset.py:301-308`) filters
`profile + visit IS NULL + organize_dismissed + pin IS NULL + wiki IS NULL + pin_suggestion IS NULL`
ordered by `-created`, on every Vault Photos load and every `refreshQueue`.

Suggested: `Index(fields=["profile", "media_type", "-created", "-id"])` plus a partial index for the
attention queue. Not added here because index creation must go last in any migration chain (see the
migration conventions in CLAUDE.md) and this deserves its own migration reviewed on real data
volumes, not a tail-end addition to a feature branch.

**Fixed 2026-09-03** by migration `0051`, `idxdb_img_profile_kind_recent` on `(profile, media_type, -created, -id)`.

Measured rather than assumed, with `EXPLAIN (ANALYZE, BUFFERS)` over 200k rows across 20 profiles against a table carrying the four indexes it joins: the page query drops from **1,881 buffers to 6**, its `count()` from **1,881 to 61**, and both become heap-free index-only scans. The oldest-first sort plans as `Index Only Scan Backward` on the same index; the date-taken and name sorts order by an expression and cannot use it.

One caveat the numbers depend on: an index-only scan needs the visibility map, so on a table autovacuum has not reached the planner still picks the old bitmap heap scan. Both figures above are post-`VACUUM`.

The second half of this entry - `total` recomputed on every page fetch though only needed on the first - is **not** changed. It is now a 61-buffer index-only scan rather than the 1,881-buffer heap scan that made it worth mentioning, and `total` is part of the documented `{items, total, offset, limit}` response shape that `test_album_view_ux.py` asserts on. Dropping it from later pages is an API change with a tested consumer and no measured benefit left.

## RESOLVED 2026-09-04: photo ownership was decided by the wrong column, exposing personal photos

Found by an adversarial review of the fix in `169dc5b64`, not by the change itself passing or
failing - the suite was green throughout.

Several independent gates ask one question - is this photo the profile's own picture, or somebody
else's they merely up-voted - and every one of them answered it with
`source == ImageSource.UPLOAD`: `services/wiki/concealment.py` (which rows a concealed viewer sees),
`controllers/image_gallery.py` and `external_api/views.py` (whether withdrawing from a wiki is this
person's to do), `services/media/images.py` and `partials/pins/_photo_gallery.html` (the `uploaded`
flag and its `data-uploaded` twin), `services/reputation/builtin_rules.py`, and the upload
achievements.

That is wrong in **both** directions:

- A photo picked out of the user's own Immich server, Google Photos library or Flickr account is
  their own picture, but carries the provider's name in `source`. So a concealed viewer could see a
  stranger's personal photos on a wiki. **Flickr had been in that state all along**; `169dc5b64`
  widened it to Immich and Google Photos by labelling those rows correctly.
- A row materialised from somebody else's provider search can carry `UPLOAD` anyway, because
  `media_materialize._translated_source` falls back to it for an unrecognised panel key.

Ownership is now `Image.is_own_contribution` / `ImageQuerySet.own_contributions`, three conjuncts in
one place: a `profile` is set (`services/photos/photo_enrichment` writes profile-less imagery
belonging to nobody, and an ownership test that missed that would conceal the automatic imagery a
fresh wiki is meant to show), `source` is in `ImageSource.personal_library()`, and no
`media_source_key`.

`test_image_ownership.py` asserts the personal set and its complement partition every `ImageSource`,
because the failure mode of forgetting is silent and one-directional: a new personal integration left
out shows that user's photos to a concealed viewer.

Two existing fixtures were faking a provider row without the column that makes it one, and were made
realistic rather than accommodated - a real materialised row always carries `media_source_key`.

## RESOLVED 2026-09-03: the test-runner container kept templates the repo had deleted

Found by the new block-name scan, which reported a thirteenth offending template that does not
exist on disk: `pages/memories/photos.html`, deleted on 2026-08-30 by `96180858a` ("move Memories >
Photos to a new Vault section"), was still sitting in `urbanlens_development_main_test_runner` four
days later, along with five `partials/memories/_photo_*.html` siblings from the same commit.

`bin/run_tests.sh`'s prune step existed and was correct as far as it went - but it only ever
matched `*.py`, because the reasoning behind it was about Python modules and scratch test files.
`docker cp` never deletes, so every other kind of deleted source file accumulated in the container
indefinitely. That is worse than untidy: Django's loader resolves a template *by name*, so anything
rendering `dashboard/pages/memories/photos.html` in the container kept getting a page that no longer
exists in the repository, and would have passed.

The prune and the parity check now share one `find` expression, `SOURCE_FILES`, covering `*.py`
plus `*.html` under any `templates/` directory. Deliberately not "every file": the container's tree
legitimately holds build output the host does not (compiled bytecode, collected and compressed
static assets), and an earlier attempt at pruning everything extra removed ~19,700 of them.

Same class as the still-open entry on the test-runner's compiled JS bundles going stale - that one
is the *build output* half of this, and is not addressed by this change.

## RESOLVED 2026-09-02: `OSRMGateway.base_url` has no production override - every deployment routes through the public demo server

Found while adding the egress-proxy filter entries the assistant's `distance_and_drive_time` tool
needs (`router.project-osrm.org` - see the entry above on the tools-import sweep and the sandbox
egress filter, same audit). `services/apis/routing/osrm.py`'s own docstring says "production
installs should point `base_url` at a self-hosted instance", but nothing actually wires that up:
`base_url: str = _DEMO_BASE_URL` is a bare dataclass default with no `settings.*`/env-var source,
and every caller (`services/ai/tools/routing.py`'s `_distance_and_drive_time`, and anywhere else
`OSRMGateway()` is constructed) always gets the demo server, with no way to override it short of
passing `base_url=` explicitly at each call site. The demo server is documented upstream as
dev/test-only (rate-limited, no uptime guarantee), so any real deployment's drive-time answers
depend on a service OSRM itself doesn't promise to keep available. Fix would be a
`ul_osrm_base_url: str | None` pydantic setting (mirroring `ul_openweathermap_api_key`'s pattern)
threaded into `OSRMGateway`'s `default_factory` the same way the weather gateway fix in this same
session's work handles `api_key` - not done here since it's a new setting plus deployment-docs
change, not a fix to code already touched this session.

**Fixed 2026-09-02 (2026-09-03).** `UL_OSRM_BASE_URL` (`app.py`'s `osrm_base_url`) now feeds `base_url`'s `default_factory`, the same shape the weather gateway uses for `api_key` and for the same reason - a bare dataclass default is evaluated once at import, so no later settings change reaches it. Unset, it still falls back to the demo server.

One consequence worth knowing before self-hosting: the assistant's `distance_and_drive_time` tool runs in `ai-worker` behind a deny-by-default egress proxy, so a self-hosted OSRM host has to be added to `src/urbanlens/config/egress/filter` as well. Both `.env-sample` and that file's own comment now say so.

## RESOLVED 2026-09-02: `style_suggestions.py`'s own AI-access check was never unified onto `assistant_available()`, and doing so naively would be wrong

Found auditing the AI-assistant sandboxing work (`docs/AI_PIPELINE.md`) for completeness.
`services/ai/access.py:assistant_available(profile)` was built as the one chokepoint for "may this
profile use the interactive AI assistant" - checked by the assistant's views, its context
processor, the external API, and the task itself. `services/labels/style_suggestions.py:40`
(`suggest_label_style`, unrelated to the assistant - a one-shot label icon/color suggestion via
`services.ai.factory.get_gateway()`) has its own near-identical, never-migrated check:
`user_has_feature(profile.user, SiteFeature.AI) and profile.ai_enabled and
profile.external_apis_enabled`.

Reusing `assistant_available()` here, as originally intended, would be a real behavior change, not
just a dedup: `assistant_available()` also requires `settings.UL_AI_WORKER_ENABLED` and
`SiteSettings.get_current().ai_enabled` - both specific to whether the sandboxed interactive
assistant's own `ai-worker` Celery worker is deployed. `style_suggestions.py` never touches
`ai-worker` at all; its `get_gateway()` call builds an `LLMGateway`, which resolves an inference
client via `services.ai.inference_client.get_inference_client()` - the *same* shared `ai-inference`
tier every LLM-backed feature now uses, assistant or not. So an admin who sets
`UL_AI_WORKER_ENABLED=false` to turn off the interactive chat assistant specifically (e.g. a
resource-constrained self-host that still wants auto-tagging/label-styling/import-assist to work)
would have label-style suggestions silently break too, with no way to keep them on.

Not fixed here: the plan text that called for this reuse didn't account for the inference-tier
split existing independently of `ai-worker`. The right fix, if this is worth doing, is a narrower
shared helper (`ai_features_enabled(profile)`, say) covering just the three profile/feature/
site-settings conjuncts `style_suggestions.py` already checks, with `assistant_available()` calling
that plus its own `UL_AI_WORKER_ENABLED` check - not folding `style_suggestions.py` onto the
assistant-specific function as originally planned.

**Fixed 2026-09-02 (2026-09-03)**, along the narrower line this entry argued for rather than the one originally planned. `services/ai/access.py` now exposes `ai_features_enabled(profile)` - site-wide `ai_enabled`, both profile preferences, and the `SiteFeature.AI` entitlement - and `assistant_available()` is that plus its own `UL_AI_WORKER_ENABLED` check. `style_suggestions.py` calls the shared predicate.

The conjunct it gains is the site-wide toggle, which is not a behaviour change: `get_gateway` already refused on it, one provider-gateway construction later. What it does *not* gain is `UL_AI_WORKER_ENABLED`, which is the whole point - `test_suggest_label_style_does_not_need_the_assistant_worker` and `test_ai_worker_absence_does_not_disable_other_ai_features` pin that down so a later dedup cannot quietly reintroduce it.

## RESOLVED 2026-09-01: two `Image` import tasks omit `source=`, silently defaulting to `UPLOAD`

Found while wiring VirusTotal-first scanning for externally-fetched images (`services/security/malware_scan.py`'s
`VIRUSTOTAL_ELIGIBLE_SOURCES`). `tasks.import_immich_photos` and `tasks.import_google_photos` both build their
`Image.objects.create(...)` call without a `source=` kwarg (unlike `import_flickr_photos`/`import_flickr_album_photos`,
which explicitly set `source=ImageSource.FLICKR`), so every row they create defaults to `ImageSource.UPLOAD` -
indistinguishable, to every `source`-keyed consumer, from an ordinary manual upload. That's wrong: these are picker-dialog
imports from a user's own connected Immich server / Google Photos account, not the upload form.

Doesn't affect VirusTotal eligibility either way - both `UPLOAD` and the correct `IMMICH`/`GOOGLE_PHOTOS` values are
excluded from `VIRUSTOTAL_ELIGIBLE_SOURCES` (see that constant's docstring: a user's own connected photo library is
private content, scanned by ClamAV only, regardless of how it's labeled). It does affect the Media gallery's per-source
tabs (these rows show under "Upload" instead of their own tab), `services/achievements/metrics.py`'s `UPLOAD`-filtered
upload counts, and `services/reputation/builtin_rules.py`'s `UPLOAD`-gated reputation logic - all silently miscounting
Immich/Google-Photos imports as manual uploads. Left unfixed here since it's orthogonal to the scanning change and
touches achievement/reputation counting, which deserves its own look at whether retroactively recategorizing existing
rows is warranted.

**Fixed forward 2026-09-01 (2026-09-03).** `import_immich_photos` and `import_google_photos` now pass `source=ImageSource.IMMICH` / `ImageSource.GOOGLE_PHOTOS`, with a regression test each.

**Existing rows are not backfilled, and one of the two cannot be reliably.** A Google Photos row is identifiable after the fact by its `source_url` prefix; an Immich row's `source_url` is the user's own self-hosted server, which is an arbitrary host, so no query separates those from genuine uploads. A backfill would therefore be partial by construction. Left for a decision on whether recategorising the identifiable half is worth the asymmetry - it moves achievement and reputation counts, which is why it was not done unilaterally.

**Backfilled 2026-09-04 - and two things above turned out to be wrong.**

The Immich half *is* identifiable: `ImmichAccount.server_url` is persisted, so one query per connected
account matches that account's own rows exactly. Only an account since disconnected cannot be matched,
and those rows stay under "Upload" deliberately. `manage.py backfill_personal_library_image_source`.

More importantly, "it moves achievement and reputation counts" was the wrong reason to hesitate, and
hesitating on it hid something worse. Every ownership gate in the codebase - concealment, who may
withdraw a photo from a wiki, the `uploaded` flag, reputation, the upload achievements - decided "is
this the profile's own picture" by asking `source == ImageSource.UPLOAD`. So labelling these rows
correctly *broke* those gates: a stranger's own photos on a wiki became visible to a concealed viewer.
Flickr had been in that state all along. See "photo ownership was decided by the wrong column".

With ownership moved onto `Image.is_own_contribution`, `source` no longer decides anything but the
Media gallery's per-source tabs, which is what makes the backfill cosmetic and safe.

## RESOLVED 2026-08-31: `Wiki.get_unique_search_name` is dead code

Found while adding ancestor-name qualification to `Pin.get_unique_search_name` (child pins like
"Superintendent's Cottage" now pull in the parent parcel's name/aliases so external media/web
searches stay tied to the right site - see `Pin.ancestor_search_names`).

`Wiki.get_unique_search_name` (`models/wiki/model.py`) has no callers anywhere in the codebase,
including tests - not `controllers/wiki_media.py`, not any Media-gallery provider. Every live
caller of the `get_unique_search_name` family (`services/pins/external_data.py`,
`services/apis/flickr/search.py`, `plugins/builtin/searxng_images.py`,
`plugins/builtin/gdelt.py`, `controllers/pin.py`, `tasks.py`, `controllers/spotguessr.py`,
`external_api/views_games.py`) goes through `Pin.get_unique_search_name` - the wiki media gallery
apparently resolves a viewing user's own pin at the place rather than searching from the Wiki
directly. Left unfixed here since it isn't reachable by anything, so it can't be the site of the
reported behavior; worth either wiring it up (and giving it the same ancestor-chain treatment via
`parent_wiki`/`child_wikis`) or deleting it.

**Removed 2026-09-03.** Confirmed unreachable a second time before deleting: every live call site (`services/pins/external_data.py`, `services/apis/flickr/search.py`, `plugins/builtin/searxng_images.py`, `plugins/builtin/gdelt.py`, `controllers/pin.py`, `controllers/spotguessr.py`, `external_api/views_games.py`, `tasks.py`) goes through `Pin.get_unique_search_name`, and no dynamic lookup reaches the Wiki one. The two signatures had already drifted - Pin's grew `quote_name`, `include_address` and `quote_locality`, none of which the Wiki copy ever had - which is what a shadow implementation looks like just before someone calls the wrong one.

## RESOLVED 2026-08-31: 12 non-Vault page templates declare `{% block title %}`, a block name `themes/base.html` never defines

Found reviewing Batch 6 (Vault home page) - adversarial review flagged the identical mistake newly
copy-pasted into `pages/vault/index.html`, which led to checking the rest of the codebase. `themes/
base.html`'s `<title>` tag is `{% block page_title %}{{ site_title|default:"UrbanLens" }}{% endblock %}`
(line 10) - there is no `{% block title %}` anywhere in the inheritance chain. Django silently drops a
child block whose name matches nothing in its ancestor (not an error), so every page below always
shows the site default title, never its own. Fixed the 3 Vault pages this batch touches (`vault/
index.html`, `vault/photos.html`, `vault/documents.html` - all three renamed to `page_title`), but the
same dead `{% block title %}` also sits, unfixed, in: `memories/sharing.html`, `memories/journal.html`,
`memories/maps.html`, `memories/visits.html`, `memories/locations.html`, `memories/index.html`,
`floorplans/editor.html`, `notifications/index.html`, `pin_share/detail.html`, `map/index.html`,
`map_share/detail.html`, `messages/index.html`. Fixing those is a one-line rename each
(`s/{% block title %}/{% block page_title %}/`) but touches unrelated pages/features, out of scope
for this PR - worth a dedicated small pass.

**Fixed 2026-09-03.** All twelve renamed to `page_title`. Each one extends `themes/base.html`; `themes/auth_base.html` *does* define a `title` block, so the rename had to be checked per file rather than applied by pattern.

A scan now guards the general case: `BlockNameTests.test_every_declared_block_is_defined_by_an_ancestor` (`test_page_template_integrity.py`) resolves each template's `extends` chain and fails on a top-level block name no ancestor defines. Only top-level - a block nested inside one the ancestor *does* define renders normally, and declaring a new name there is how `errors/404.html` offers `error_title`/`error_body` to `pin_not_found.html`.

The scan found a thirteenth instance that does not exist: `pages/memories/photos.html`, deleted from the repo on 2026-08-30, was still present in the test-runner container. See the entry below on the stale-template prune.

## RESOLVED 2026-08-31: `ulSectionCollapsed` is not a function - hx-trigger races the core bundle on pin detail

Seen as 28 identical console errors during a Playwright run against the dev environment:
`TypeError: (intermediate value)(intermediate value)(intermediate value).ulSectionCollapsed is not a function`.
`window.ulSectionCollapsed` is assigned by `shared/collapsible-sections.ts:231` (bundled into
`core.js`), and is read from `hx-trigger="load[!window.ulSectionCollapsed('pin','...')]"` attributes
in `partials/pins/_pin_location_data_tabs.html:35`, `_pin_plugin_tabs.html:40`,
`pages/location/index.html` (×8) and `pages/location/wiki.html` (×4). When htmx evaluates those
`load` triggers before `core.js` has executed, every one of them throws and the section silently
never loads its content.

Unrelated to the Vault (the string appears in no Vault or album template) - surfaced only because a
Vault album spec navigates to a Private Pin page. Worth guarding the trigger expression
(`window.ulSectionCollapsed && !window.ulSectionCollapsed(...)`) or asserting load order.

**Fixed 2026-09-03**, across all 20 call sites in four templates.

The guard's polarity is the part worth recording. The obvious form - `window.ulSectionCollapsed && !window.ulSectionCollapsed(...)` - evaluates falsy when the global is missing, which converts a thrown error into a section that silently never loads: the same broken page, minus the console evidence. The shipped form is `!(window.ulSectionCollapsed && window.ulSectionCollapsed(...))`, so an absent global reads as *not collapsed* and the section loads. A section that fetches while collapsed is invisible and correct; one that never fetches is a blank panel.

`collapsible-sections.contract.test.ts` scans the template tree for both the unguarded call and the inverted guard, and checks each filter's parentheses balance (htmx tracks bracket depth when splitting a trigger spec).

Confirmed against real htmx 1.9.11 rather than by reading, in a throwaway harness that loaded the
pinned bundle into a DOM and watched the requests it did and did not issue: the guarded filter
parses, fires when the global is absent, defers when it reports collapsed, fires when it reports
expanded, and leaves the sibling `ul:unhide` trigger in the same spec working. The same harness
reproduced the original failure exactly - the unguarded form raises
`ulSectionCollapsed is not a function`, and htmx swallows it into "do not fire", which is why the
symptom was a blank panel rather than an error the user could see.

## RESOLVED 2026-09-03: `UL_METRICS_ENABLED=true` on an image without django-prometheus crash-loops every Django process

Reproduced, not theorised: `docker exec urbanlens_development_main_celery_worker
python -c "import django; django.setup()"` on an image built before
`django-prometheus` entered `pyproject.toml` dies with a bare
`ModuleNotFoundError: No module named 'django_prometheus'`.

`settings/base.py` appends the app to `INSTALLED_APPS` and its two middlewares
whenever `UL_METRICS_ENABLED` is on, with no guard on whether the package is
importable and none on process role. That is fine when image and settings ship
together, which is the normal path. It is not fine because **the env var is
routinely changed independently of an image build** - flipping metrics on for an
existing deployment takes down app, app-ws, beat and all four workers at once,
with an error that names a missing module rather than the setting that required
it.

Two separable fixes, neither done:

1. Raise `ImproperlyConfigured` naming `UL_METRICS_ENABLED` when the import
   fails, so the operator sees the cause instead of the symptom.
2. Gate the app and middleware on process role. Only web processes serve
   `/metrics`; workers register the middleware and never run it. `UL_PROCESS_ROLE`
   already exists for exactly this kind of distinction, but is defined further
   down the settings file than the `INSTALLED_APPS` block, so this needs a small
   reordering rather than a one-line change.

Found while verifying the Celery requeue fix, from the metrics work in
`7b5c55236` - i.e. this is a gap in that change, not a pre-existing one.

**Fixed 2026-09-03.** Both halves, in `settings/_metrics.py`. `instrumentation_wanted()` narrows registration to the roles whose Django stack is actually scraped (`web`, and `unspecified` for a local checkout or `runserver`), so app-ws, beat and the four workers no longer import a package they have no use for; `require_django_prometheus()` turns a missing package into an `ImproperlyConfigured` naming `UL_METRICS_ENABLED`. No reordering was needed after all - `INSTALLED_APPS` is built after `_app_settings` is imported, so the role was already readable there.

Verified in the container rather than only in tests, with `django_prometheus` blocked by a meta-path finder: `UL_PROCESS_ROLE=worker` now reaches `django.setup()` (`instrumented=False`), and `UL_PROCESS_ROLE=web` raises the named error. `UL_METRICS_INSTRUMENTED` is the derived setting the middleware block reads; `settings/test.py` pins it off alongside the flag it is derived from.

## RESOLVED 2026-09-02: nine `Gateway` subclasses read a credential setting once at import time, not per-instantiation

Found while adding the assistant's `get_weather` tool (`services/ai/tools/weather.py`), and confirmed as a live bug -
not just a test artifact - via a real test failure: `test_weather_resolution.py`'s
`test_openweathermap_configured_takes_priority_over_open_meteo_on_direct_path` started failing once a new,
unrelated test file (`test_ai_tools_weather.py`) happened to import `services.apis.weather.gateway` earlier in the
same process while `settings.openweathermap_api_key` was mocked to `None`, poisoning the value for every later
`OpenWeatherMapGateway()` in that process regardless of what the settings object held afterward.

Root cause: `api_key: str | None = settings.openweathermap_api_key` (and siblings) is a bare `@dataclass` field
default, evaluated exactly once - at class-definition/module-import time - not per-instantiation. Fixed by
switching every live instance of the pattern to `field(default_factory=lambda: settings.the_attr)`, which re-reads
the current value on every call, in: `services/apis/weather/gateway.py` (`OpenWeatherMapGateway`),
`services/apis/property_records/redata_gateway.py` (`RedataGateway`),
`services/apis/photos/redata_photos_gateway.py` (`RedataPhotosGateway`),
`services/apis/locations/google/redata_cid_gateway.py` (`RedataCidGateway`),
`services/apis/locations/google/redata_places_gateway.py` (`RedataPlacesGateway`),
`services/apis/labels/redata_labels_gateway.py` (`RedataLabelsGateway`),
`services/apis/locations/redata_context_gateway.py` (`RedataLocationContextGateway`),
`services/apis/security/virustotal.py` (`VirusTotalGateway`), `services/apis/messaging/sms.py` (`SmsGateway`),
`services/apis/messaging/whatsapp.py` (`WhatsAppGateway`) - all nine are themselves `@dataclass`-decorated and
redeclare `base_url`/`api_key`/`account_sid`/`auth_token`/`from_number` as genuine dataclass fields with the same
bare-default shape.

One tenth site, `services/apis/redata_json_gateway.py`'s `RedataJsonGateway`, has the identical-looking
`base_url`/`api_key` lines but is **not** itself `@dataclass`-decorated (it's a plain `Gateway` subclass) - those
lines are ordinary class attributes, not dataclass fields, and are always shadowed by the redeclared dataclass
fields on its two concrete subclasses (`RedataLabelsGateway`, `RedataPhotosGateway`), which are never constructed
by passing `base_url=`/`api_key=` explicitly. `RedataJsonGateway` itself is never instantiated directly anywhere
in the codebase (confirmed by grep), so this copy is dead/inert rather than a tenth live bug - left as-is; a
`field(default_factory=...)` edit there would be silently inert too (dataclass processing never runs on this
class) and touching it would misleadingly suggest it does something.

In production this was low-risk (settings are effectively static per worker process, and REData/Twilio/VirusTotal
keys are rarely rotated without a restart), but it meant a live key rotation, a settings reload, or - the case that
actually surfaced it - a test process where mock-patching order and first-import order interact, would silently
use a stale credential instead of the current one.

## RESOLVED 2026-08-31: `OvertureMapsGateway` scanned the entire planet's buildings dataset per lookup, OOM-killing Celery workers

Found investigating a host-wide (`chiron`) out-of-memory emergency: `celeryd` prefork children in
two independent dev sandboxes (`urbanlens_agent_aa4df6e`, `urbanlens_agent_ae97b86` - unrelated
checkouts of this same repo) had each grown to 4.5-5.4GB RSS, repeatedly triggering the kernel OOM
killer (confirmed via `dmesg`/`journalctl -k`: `Out of memory: Killed process ... ([celeryd: celer)`,
one victim every few minutes), with the host down to ~500MB free and swap fully exhausted. This
starved every other project sharing the host, not just UrbanLens.

Root cause: `OvertureMapsGateway._fetch()` (`services/apis/locations/boundaries/overture_maps.py`)
called `overturemaps.geodataframe()` without `stac=True`. That parameter gates whether the official
Overture client first resolves a bbox against Overture's small STAC-geoparquet index to the handful
of S3 partition files that actually intersect it; without it, `pyarrow.dataset.dataset()` opens the
*entire* theme (every partition file in the release) and relies on filter pushdown alone while
scanning. Verified live against the `2026-08-19.0` release: a ~111m lookup bbox
(`BOUNDARY_LOOKUP_BBOX_DEGREES = 0.001`, the actual size every caller here uses) resolves to **1**
intersecting file via STAC vs. **512** files in the unfiltered dataset - global buildings-theme
partitions run into the multiple gigabytes each. This is called from `auto_nest_building_pins`,
`classify_detail_marker`, boundary generation, and the `OvertureBuildingAttributesPanelSource`
pin-detail panel - all frequent, small-bbox, single-building lookups that should each read a few MB,
not scan the whole planet. `test_overture_building_attributes_budget.py`'s docstring already had a
second, independent symptom on record ("observed in production taking ~50s+ each back-to-back") that
nobody had connected to this until now.

Compounding factor, not the root cause: `CELERY_WORKER_MAX_TASKS_PER_CHILD`/
`CELERY_WORKER_MAX_MEMORY_PER_CHILD` were both unset, so a prefork child that hit this once never
recycled - its bloat was permanent for the life of the process, and with no bound on the pool it
took several such hits (spread across the pool by chance) to exhaust the host rather than one.

**Resolved 2026-08-31.** `_fetch()` now passes `stac=True`; regression guard in
`test_overture_maps_stac_narrowing.py` asserts it. `CELERY_WORKER_MAX_TASKS_PER_CHILD=200` and
`CELERY_WORKER_MAX_MEMORY_PER_CHILD=512MB` (`settings/base.py`, both env-overridable) now recycle a
child that balloons for any reason - defense in depth, not a fix for this specific leak, since
imports/allocator fragmentation in a long-lived prefork worker only ever grow and nothing else
returns that memory to the OS. Immediate relief: the two OOM-cycling worker containers (plus their
sibling app/panels/beat/app-ws containers, so no process kept the pre-fix module cached in memory)
were restarted, reclaiming ~19GB back to the host (500MB free -> 20GB free, swap 4.0/4.0GB used ->
dropping) within seconds - confirming the fix rather than the restart was what mattered, since the
same containers stayed flat afterward instead of re-climbing.

**Follow-up, same day:** `docker-compose.yml`'s `mem_limit`/`cpus` had just been added for every
service (per-service `${MEM_LIMIT__X:-${MEM_LIMIT:-...}}` override pattern already in place) but were
all left at one unexamined blanket default - `4g`/`4 cpus` for every service regardless of workload,
except `clamav` (already `1g`/`2 cpus`) and `valkey` (`24g`, an unused placeholder - `valkey` is itself
invoked with `--maxmemory 512mb`, so the container ceiling could never matter). Audited via ten
parallel per-service research passes grounded in this incident's live measurements (idle RSS for every
service on this exact stack) plus technology-specific reasoning (gunicorn worker count, Daphne's
no-fan-out blast radius, nginx's hardcoded `worker_processes 2`, Postgres's `CONN_MAX_AGE=0` meaning
connection count - not idle RSS - drives its real ceiling, `celery-worker`'s prefork-vs-thread-pool
distinction from `celery-worker-panels`, clamd's signature-reload spike, pytest-xdist's real-subprocess
memory multiplication). Result: `app` 4g->1536m, `app-ws` 4g->768m, `nginx` 4g->128m, `db` 4g->2g,
`celery-worker` 4g->3g (sized with this incident specifically in mind - see above), `celery-worker-panels`
4g->1g, `celery-beat` 4g->512m, `clamav` 1g->2g (the *old* 1g was actually undersized - measured
steady-state was already 93-100% of it, before daily signature-reload's transient spike), `valkey`
24g->1g, `test-runner` 4g->6g, `test-db` 4g->2g, `test-valkey` 4g->512m - all still overridable via the
same `MEM_LIMIT__<SERVICE>`/`MEM_LIMIT` env vars. Worst-case simultaneous total (every service at its
new ceiling at once) is ~20.4GB across all 12 services including the test profile, down from ~65GB
under the old blanket defaults.

Noted but *not* changed here (out of scope for a limits audit): none of the 9 always-on services carry
a `profiles:` key, so `docker compose --profile test up` (without naming services) starts them
alongside `test-runner`/`test-db`/`test-valkey`, not instead of them - a redundant `db`+`test-db` and
`valkey`+`test-valkey` pair, and the real worst-case total for one stack instance running its own test
profile is the full ~20.4GB, not two separate smaller scenarios. The file's own documented usage
(`docker compose --profile test up -d --build test-runner test-db test-valkey`, naming services
explicitly) avoids this in practice, since Compose only starts named services plus their `depends_on`
- but running the bare `--profile test up` flag without following that convention hits it.

## RESOLVED 2026-08-29: the external API leaks a full Django debug page on any `Accept: text/html` request

Found calibrating the new sqlmap integration (`bin/run_sqlmap_scan.sh`) against a real dev container -
sqlmap's WAF-bypass mode sends browser-like headers, including `Accept: text/html,...`, which a normal
API client wouldn't. Any external API request that content-negotiated to HTML crashed with
`TemplateDoesNotExist: rest_framework/api.html`: `rest_framework` was never added to `INSTALLED_APPS`,
so DRF's default `BrowsableAPIRenderer` - on by default alongside `JSONRenderer` whenever
`DEFAULT_RENDERER_CLASSES` isn't overridden - can never find its own template. In an environment where
`DEBUG` resolves true, that 500 shipped with a full Django debug page in the response body: settings
values (internal Valkey/Redis hostnames, CSP configuration), the traceback, the works.

**Resolved 2026-08-29.** `REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"]` (`settings/base.py`) is now
`JSONRenderer` only, correct independent of the crash - this is a machine-consumed API
(`docs/EXTERNAL_API.md`) with no working browsable UI to begin with; the actual interactive explorer is
the separate Swagger UI view. Verified live against the dev container: `Accept: text/html` now answers
a clean `406` instead of a 500. See `docs/INTEGRATION_TESTS.md`'s sqlmap section, "What the first live
calibration run found," for the other two (sqlmap-side, not application-side) bugs the same run found.


## RESOLVED 2026-08-25: two views read `request.user.profile` on a possibly-anonymous user

`controllers/map_overlays.py` (then :320, now :329) and `controllers/safety.py:1228` both called
`request.user.profile` where mypy types `request.user` as `User | AnonymousUser`. Surfaced by
running mypy across the whole `controllers/` package rather than a file at a time, which is not
routine here.

Not live: both are on `LoginRequiredMixin` views, so `request.user` is always authenticated by the
time either line runs. It is a typing lie rather than a crash, and the trap is that removing the
mixin - or reusing either helper from an unauthenticated path - turns it into an `AttributeError`
with nothing pointing at the cause.

The fix is not a cast. Every other view resolves the viewer as
`Profile.objects.get_or_create(user=request.user)` (see `services/wiki/wiki_access.resolve_visible_wiki`),
which is honest about both the type and the possibility that the profile does not exist yet. Left
alone here because these two views are unrelated to the work that surfaced them and the change is
behavioural, not cosmetic.

**Resolved 2026-08-25.** Both now resolve the viewer as
`Profile.objects.get_or_create(user=request.user)`, the same way every other view does - which is
honest about the type and about the profile possibly not existing yet. `mypy` is clean across all
85 files in `controllers/`; 53 tests over the two views pass.


## ~~2026-07-28: `Friendship.muted` is shared by both profiles, not per-viewer~~ - RESOLVED 2026-08-20

~~There is exactly one `Friendship` row per pair, so the `muted` boolean added in migration
`0020_friendship_muted_flag` was a property of the *relationship*: if A muted B, B's own view read
`muted=True` too.~~

Migration `0057_directional_friendship_mute` splits it into `muted_by_from_profile` /
`muted_by_to_profile` - the two-column option this entry recommended, kept over a separate
`FriendshipMute(viewer, target)` model because mute is only meaningful on an existing relationship
(`mute_profile` refuses a stranger, deliberately), so a standalone table would have permitted a
state the service layer exists to prevent.

Read them through `Friendship.is_muted_by(viewer)`, never directly; the columns are named for the
row's two ends, and which one belongs to a given viewer is exactly the detail a caller gets wrong.
`is_muted_by` **raises** for a profile that is not part of the row rather than answering False -
every wrong answer here silences somebody who did not ask to be. The queryset filters are
`muted_by(viewer)` / `not_muted_by(viewer)`, replacing the viewerless `muted()` / `unmuted()`.

Existing `muted=True` rows carry no record of who set them and it cannot be recovered, so the data
migration sets **both** sides. That is what each of the two people currently sees: the profile page
read the shared flag, so both were already shown "Muted" and offered "Unmute". Either can now clear
their own half.

One casualty worth naming: `test_friendship_mute_flag.py`'s `LegacyMutedRowRepairTests` ran
migration `0010`'s repair callables against the *live* app registry, which was only valid while the
historical `Friendship` was field-identical to the current one. It no longer is, so those four
DB-executing tests are replaced by a `SimpleTestCase` that pins the migration still wires both
directions to the real functions - the property that a `RunPython.noop` reverse would break.

## ~~2026-07-28: `Friendship.muted` is stored but nothing reads it - muting a friend silences nothing~~ - RESOLVED 2026-08-20

~~The profile page's Mute button, and the external API's `POST /friends/{uuid}/mute/`, both record a
preference that no delivery path honours.~~

Fixed as this entry proposed - a single helper consulted from one place - but the place turned out to
matter more than the helper. There were ~30 `NotificationLog.objects.create(...)` call sites, so
"consult it where `NotificationLog` rows are created from a `source_profile`" meant remembering the
rule thirty times, and a notification type added later could not inherit a rule that lived nowhere.
That is the shape that produced the bug in the first place, and re-implementing it would have
produced the next one.

So:

- `NotificationLog.objects.notify(**fields)` is the sanctioned producer. It calls
  `services.social.friendship.notifications_muted(recipient, source)` and returns `None` instead of
  a row when the recipient muted the source. All ~30 production call sites now use it.
- `bin/check_notification_choke_point.py` (wired into pre-commit) fails the build for any
  production `NotificationLog.objects.create/bulk_create/get_or_create/...` or bare
  `NotificationLog(...)`. Tests are exempt; a deliberate bypass is marked `notify-bypass-ok: <why>`
  on the line above.

Suppressing the *row* is what produces silence, not merely a quieter bell: the live WebSocket
toast, the delayed WhatsApp/SMS alert and the native push are all `post_save` receivers on
`NotificationLog` (`models/notifications/signals.py`). **Not covered:** emails a producer sends
directly alongside its notification never passed through the manager, so they still go out. Worth a
follow-up; it is a smaller and much more visible surface than thirty producers.

Callers that store the row (`PinShare.notification`, `MarkupMapShare.notification`,
`VisitSuggestion.notification`) already tolerate `None` - that FK is nullable because a recipient
can switch the type off entirely, which produces the same outcome.

**The safety check-in family is exempt** (`MUTE_EXEMPT_TYPES` in
`models/notifications/meta/type.py`). Mute is a volume control on someone's social activity; a
preference set weeks ago about a friend's chatter is not consent to stop being told they have not
come back from a site. The partner invite/accepted pair is exempt for a related reason - it is an
actionable request whose sender would otherwise wait for an answer nobody was asked for. The list
is written out rather than derived from the `safety_ci_` value prefix, and
`test_friendship_mute_suppression.py` fails when a new safety type is added without a decision.

Two consequences worth stating, both deliberate:

- Mute now also suppresses group-chat message notifications from that person, on top of the
  narrower `DirectMessageMute` and per-group mute, which still apply independently.
- `remove()` keeps the row, so a mute outlives the friendship: if the pair reconnect (`request()`
  reuses the row) the muter stays un-notified until they unmute. Silently restoring someone you
  muted is the worse surprise.

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

### (ORIGINAL) 2026-07-30: SearXNG (`search.jmann.me`) image search 403s after coming back up from an outage

Same production log sweep as the entry above. `search.jmann.me` was confirmed down (DNS resolution
failures) earlier the same evening; once the operator brought it back up, `SearxngGateway.search`/
`search_images` started getting `403` instead. `searxng.py` never set a `User-Agent` (defaulting to
`python-requests/x.y`, a common bot-signature block target), so a `User-Agent` header was added as a
cheap, safe mitigation (`searxng.py`'s `_USER_AGENT` constant, set in `__post_init__`). Not confirmed
as the actual cause - equally plausible is the instance's own `limiter.toml` bot-detection or a
fronting WAF/CDN (e.g. Cloudflare) rule that changed when the service was brought back online. If
403s persist after the User-Agent change, check the instance's own access/limiter logs server-side -
this session had no access to `search.jmann.me`'s host.

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

### (ORIGINAL FILING) 2026-08-13: "detach location" on a pin fails with a 500, every time

`controllers/pin_edit.py:631` (the `else` branch of the location-change handler, reached when the
user detaches a pin from its shared `Location`) does:

```python
lat = float(pin.effective_latitude or 0)
lng = float(pin.effective_longitude or 0)
location = Location.objects.create(official_name=..., latitude=lat, longitude=lng)
```

`Pin.effective_latitude` is `float(self.location.latitude)` - the pin's **current** location's
stored coordinate. `Location` is `unique_together = ("latitude", "longitude")` (declared in
`0001_initial`, so this is not a regression from recent work). Creating a Location at coordinates a
Location already occupies is therefore a guaranteed constraint violation.

Reproduced directly, not inferred:

```
pin.effective_latitude=42.1234  location.latitude=42.1234
detach create: IntegrityError -> duplicate key value violates unique constraint
               "dashboard_locations_latitude_longitude_fdb6594d_uniq"
```

Both branches fail identically - the named-location branch above, and the fallback
`_create_location_with_canonical_name(lat, lng)`, which ends in the same bare
`Location.objects.create` (`controllers/maps.py:1156`).

**The fix is a product decision, which is why this is filed rather than patched.** `Location` is a
*shared* record of a physical place, globally unique on its coordinates - so "give this pin its own
Location at the same point" is not expressible. What "detach" should do instead is a design
question with at least three defensible answers:

- nudge the new Location's coordinates by the smallest representable amount, giving a genuinely
  distinct point (changes where the pin sits, slightly);
- keep one Location and express the separation another way - the pin already has marker-coordinate
  fields, which may be what "detach" was reaching for;
- refuse with an explanation, if two users pinning one physical place is *supposed* to share a
  Location and detaching was never coherent.

Whichever is right, the current behaviour - an unhandled `IntegrityError` surfacing as a 500 - is
wrong under all three.

**Why it went unnoticed: the branch has no test.** `PinRelinkView` serves two routes - `pin.link.to`
(relink to a named Location, which *is* covered, by `test_pin_relink_access.py` and
`test_pin_location_conflict.py`) and `pin.link` (detach, no location slug). Searching the whole test
tree for the detach route returns nine hits, and every one is `pin.link.delete` - an unrelated
endpoint for removing a pin's external links. Nothing posts to `pin.link`.

So this is not a subtle conditional that testing missed; the code path is simply never executed.

**Test added 2026-08-14 (audit chunk 326)**, `tests/hypothesis/test_pin_detach_location.py`. It
posts to `pin.link` and is marked `xfail(strict=True)` - it does **not** assert the 500, because
that would cement the bug as intended, and it does not presuppose which of the three fixes is
right. When detach stops raising, the strict marker fails and tells whoever fixed it to replace
the marker with a real assertion. The product decision above is untouched and still yours.
Whatever the fix turns out to be, a test posting to `reverse("pin.link", args=[pin.slug])` belongs
with it - that single request is enough to catch this class permanently.

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

### (ORIGINAL FILING) 2026-08-17: blocking leaves a saved emergency-contact default pointing at the blocked profile

Found alongside the `MarkupMapShare` revocation, and left for an owner's decision because it is a
product question rather than a defect.

`EmergencyContactDefault` is a *template*: it is copied onto each new `SafetyCheckin` as a
`SafetyCheckinContact` snapshot at creation time. Blocking someone does not remove it, so a check-in
created *after* the block still copies the blocked profile in as an emergency contact - and the
safety escalation path will page them.

Both answers are defensible, which is why this is filed rather than fixed:

- **Remove it on block.** Consistent with how blocking already treats safety-partner access, and
  avoids the surprise of paging someone you blocked.
- **Leave it.** The default is the owner's own saved data, and silently deleting a safety contact is
  destructive in a feature whose whole purpose is that someone is notified when you do not check in.
  An owner might block a person socially and still want them called if they go missing.

A third option, probably the best of the three: keep the row, and warn at check-in creation when a
default resolves to a blocked profile - which informs without deciding for them.

Everything else that links two profiles was enumerated while checking this (twenty models). The rest
are either already handled (`Friendship`, `SafetyCheckinPartner`, pending `PinShare`,
`DirectMessageTemporaryAccess`'s read-time veto), private annotations the author owns
(`ProfileNote`, `ProfileNickname`, `ProfileTrust`, `ProfileLabelAssignment`), or deliberately
preserved history (`DirectMessage`, `ConversationKey`).

## RESOLVED 2026-08-18: importing buildings on a *pin* page 500'd on the wiki side

Reported from staging with a traceback: adding several buildings from the Private Pin page raised
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

---

# Archived 2026-08-19: the 2026-08-06/08 full-codebase audit, and entries resolved before it

Moved verbatim out of `docs/PROBLEMS.md`, which had grown to 5,934 lines of which most was
session history whose findings were fixed in the same session. Nothing here was edited on the
way across. Twenty-plus of the "fixed" claims below were re-verified against the tree before
the move; the ones that turned out **not** to be fixed stayed behind in `PROBLEMS.md` (see the
export.py N+1 regression, which a merge had silently reverted).

## RESOLVED (verified 2026-08-19): 16 pre-existing failures outside every prior sweep's `-k` filter

A full unfiltered run on this branch is green (11,436 passed / 0 failed, 2026-08-18). Whatever these
were, they are not failing now. Original filing follows for the triage notes.

### (ORIGINAL) OPEN 2026-07-28

Every sweep so far this session used a `-k` keyword filter scoped to pin/wiki/location/friend/
external-API territory. A full unfiltered run (`pytest src/urbanlens/dashboard/tests`, no `-k`,
~70 minutes) surfaces a different, previously-unswept **16 failed, 7990 passed, 2 xfailed**. None
of the 16 files were touched by anything in this session (last-commit dates 2026-07-22 through
2026-07-25, `git log -1` per file) or relate to pins/wikis/locations/friends/the external API -
this is a fresh, disjoint backlog, not a regression from today's work. **Only triaged, not fixed.**

Two entries from the raw 18-failure sweep output are not real and should be discarded outright if
re-seen: `test_zzz_client_probe_tmp.py` was a throwaway file created and deleted mid-session for an
unrelated probe (see the `@given`+`self.client` CLAUDE.md entry) - the long-running sweep's
collection phase had already imported it before the delete, so it ran from memory once, near the
end of the ~70-minute run, off a module that no longer exists on disk. And
`test_spotguessr_geo_bonus.py::BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising`
is order-dependent pollution from the full run - it **passes standalone**, unlike the 16 below,
which were re-verified to fail in isolation too (`16 failed, 1 passed` re-running just this set).

Root-caused from a `--tb=short` capture, not yet fixed:

1. **Not a bug - a run that crossed midnight.** `test_global_search_parser.py::
   ParseQueryStructureTests::test_this_year_ends_today` failed with
   `datetime.date(2026, 7, 28) != datetime.date(2026, 7, 27)` - the assertion computed "today" at
   two different points ~70 minutes apart in a suite run that happened to straddle midnight. Not
   reproducible on demand; would pass on any run that doesn't cross a day boundary mid-execution.
2. **Unmocked live network calls** (the same class of bug fixed repeatedly earlier in this file):
   `test_loopnet.py::FetchTests::test_unconfigured_gateway_gracefully_persists_empty` and
   `test_trip_ai_suggestions.py::ApplySuggestedOrderViewTests::test_valid_permutation_applies` both
   raised `RuntimeError: External network access is disabled during tests` against real IPs
   (`10.2.0.214`, `5.148.170.168`). Check whether each is "test forgot to patch, sibling tests
   show the pattern" (most likely, per every prior instance of this bug this session) or a
   genuine missing gate in the view/service.
3. **Test-only bug.** `test_redata_cid_gateway.py::RedataCidGatewayResolveCidsTests::
   test_non_200_raises_gateway_request_error` - `TypeError: 'Mock' object is not subscriptable`.
   The mock response object isn't shaped to support whatever subscript the code under test uses;
   compare against a sibling test in the same file that mocks correctly.
4. **Possibly Windows-specific.** `test_backup_services.py::BackupFilesTests::
   test_returns_only_files_sorted_by_mtime_descending` (`[] != [WindowsPath(...new.sql),
   WindowsPath(...old.sql)]`) and `CollectBackupStatsTests::test_collects_count_latest_size_and_settings`
   (`0 != 2`) both show the service finding zero files where the test created two - the service's
   discovery glob/pattern likely doesn't match on Windows path separators, or looks in a path this
   test's temp dir isn't under. Worth checking whether this fails in Docker/CI too before assuming
   Windows-only.
5. **Possibly a real permission bug, worth prioritizing.** `test_trip_ai_suggestions.py::
   TripAiSuggestionsViewTests::test_non_member_is_rejected` got `404` where it expected `403` - on
   its face this reads like the uniform-404 privacy pattern used elsewhere in this codebase
   (wiki discovery, trip detail - see the resolved trip-controller entry above), in which case the
   test is stale and the code is behaving correctly. But it could also be a plain "wrong pin/trip
   ownership lookup" bug that happens to 404 for the wrong reason. Read `TripAiSuggestionsView`
   before assuming either way.
6. **Hypothesis-found edge case, may be a real off-by-one.**
   `test_searxng_image_query.py::AssembleImageQueryTests::test_group_count_matches_present_components`
   failed on `aliases=['0'], area=['(']` with `4 != 3` - a generated alias/area combination made
   the query-group count disagree with the number of query components actually present. Worth a
   look at `assemble_image_query`'s group-counting logic directly rather than reverse-engineering
   it from the failing example.
7. **Assertion mismatches not yet read in detail** - each needs its own traceback before triage:
   `test_avatar_colors.py::GroupMemberSearchAvatarColorTests::test_results_get_distinct_colors`
   (`0 != 4`), ~~`test_dm_search.py::SearchDirectMessagesTests::
   test_date_range_phrase_filters_by_created` (`[] != [1]`)~~ **FIXED 2026-08-05, see below**,
   ~~`test_global_search_engine.py::PhotoSearchTests::test_finds_photo_by_generated_keyword`
   (expected string not found in an empty result list)~~ **FIXED 2026-08-05, see below**,
   `test_media_own_photos_preview.py::PhotosMediaPreviewTests::` both
   `test_own_photo_tile_carries_image_id_and_coordinates` and
   `test_own_photo_tile_without_coordinates_renders_empty_lat_lng` (`204 != 200` - both in the
   same class, worth checking whether one shared fixture broke both),
   `test_settings_tos_accepted_display.py::SettingsTosAcceptedDisplayTests::
   test_shows_the_acceptance_date_when_recorded` ("Mar 4, 2025" not found in the rendered page -
   possibly the same CSRF-token/date-formatting class of stale-assertion bug fixed elsewhere in
   this file), and `test_trivia_stall.py::ForceRevealRoundTests::` both
   `test_a_partial_answer_is_revealed_and_only_the_answerer_is_rated` (`'active' !=
   TriviaSessionStatus.COMPLETED`) and `test_advances_to_the_next_round_when_more_remain`
   (`1 != 2`) - both in the same class, likely one shared root cause in the force-reveal flow.

Reproduce with (no `-k` filter, so the full ~70-minute runtime applies):
```
pytest src/urbanlens/dashboard/tests -q --reuse-db --tb=short
```
Re-running just the 16 test IDs above in one invocation reproduces all of them in ~5 minutes.

## ~~SiteSettings.ai_article_expansion_enabled / ai_article_safety_enabled have no migration~~ (found and fixed 2026-07-25)

While generating a migration for an unrelated new `SiteSettings` field
(`ai_trivia_wiki_incorporation_enabled`, Trivia Phase 4), `manage.py makemigrations` reported
these two fields as pending additions even though both are already defined on the model and have
been in use since the "Implement AI article expansion and safety review features" commit. The
migration squash ("squash into single migration for release") apparently ran before that commit's
own migration was authored, or that migration was never committed - every test that creates a
`SiteSettings` row (effectively the whole suite, via `promote_first_user_if_needed`'s
`SiteSettings.objects.get_or_create`) failed with `ProgrammingError: column
dashboard_site_settings.ai_article_expansion_enabled does not exist` on a freshly migrated DB.
Initially left out of Trivia Phase 4's own migration as out-of-scope, but ended up blocking that
same phase's dev-pod verification (wiki incorporation directly reuses the article-expansion
pipeline), so it was folded into
`0011_trivia_wiki_incorporation_setting.py` alongside the Trivia field rather than filed away
again - see that migration's own comment.

## A few more hardcoded danger-red controls without dark overrides in `_pin_lists.scss` (found 2026-07-25)

While fixing the `.saved-filter-region-mode-btn--active` include/exclude buttons (raw
`#2e7d32`/`#c62828`, now real `--ul-color-success-text`/`--ul-color-danger-text` tokens), noticed
several sibling controls in the same file with the identical pattern that were **not** in scope
for that fix: `.pin-list-more-menu-danger` (`color: #ef4444 !important`, ~line 325),
`.saved-filter-delete-btn` (`color: #ef4444`, ~line 629), and a related hover state at ~line 521
(`color: #fca5a5`). None has a `[data-theme="dark"]`/`_dark.scss` counterpart. Given
`--ul-color-danger-text` now exists and already resolves correctly in both themes, these three
are likely a quick follow-up: swap the raw hex for `var(--ul-color-danger-text, <original-hex>)`
the same way the region-mode buttons were fixed.

**RESOLVED 2026-08-17 (chunk 582).** All five occurrences across the three controls now use the
token, keeping their original hex as the fallback. `--ul-color-danger-text` is defined in `:root`
and again under `[data-theme="dark"]` (`#f87171`), so it resolves in both themes as this entry
said. The `#fca5a5` hover on `.pin-list-item-remove` was worth a second look rather than a
mechanical swap: it is a *pale* red, chosen for a dark surface, on a control whose rest state
already inverts through `--ul-grey-4` - so it was a light-mode contrast problem, not a deliberate
lighter-on-hover treatment. Compiled clean with `sass`; the built CSS is gitignored
(`.gitignore:9`), so the source change is what ships.

## `test_post_without_name_returns_400` is stale against UL-360's optional-name behavior

`test_trip_controller.py::TripCreateViewTests::test_post_without_name_returns_400` (line ~224)
posts `{"name": ""}` to `TripCreateView` and asserts a 400. Found failing during the pod
verification of an unrelated trips-list-page/safety-checkin feature (2026-07-25) - not touched by
that work. `TripCreateView.post` (`controllers/trip.py`) already has: `name = (body.get("name")
or "").strip() or random_trip_name()` - per its own "Name is optional (UL-360)" comment, a blank
submission has deliberately generated a random name instead of rejecting the request since UL-360
shipped (2026-07-24, see the Feature build entry above). The test predates that change and was
never updated; it should either assert `200` + a non-empty generated `trip.name`, or be deleted
if UL-360's own test coverage (`test_trip_names.py`?) already covers the generated-name path.

---

## `test_spotguessr_geo_bonus.py` leaks a real Valkey cache entry across tests (found 2026-07-26)

Found while running the full Consensus/SpotGuessr regression suite alongside the new Facts
system tests (unrelated to Facts - `services/spotguessr/geo_bonus.py` was never touched).
`BonusPointsForGuessTests::test_geocode_failure_earns_nothing_without_raising` fails with
`AssertionError: 750 != 0` even run alone in its own file (`docker compose exec test-runner
python -m pytest src/urbanlens/dashboard/tests/hypothesis/test_spotguessr_geo_bonus.py`, 1
failed / 8 passed) - so it's not cross-file pollution, it's within-class.

`bonus_points_for_guess` -> `_reverse_geocode_admin_cached` (`services/spotguessr/geo_bonus.py:157`)
caches Nominatim's admin lookup in the real Valkey cache keyed by rounded coordinates, and the
test class never clears that cache between tests. `test_matching_every_offered_tier_stacks_the_bonus`
and `test_no_match_at_all_earns_nothing` run earlier in the same file against the same
`guess_point = Point(-73.75, 42.65, ...)` used by the failing test, so whichever of those wrote a
cached "match" result first is still there when the geocode-failure test runs - the mocked
`NominatimGateway` returning `None` never gets consulted because the cache short-circuits it.
Fix is a `self.enterContext` cache-clearing fixture (or `cache.clear()` in `setUp`) in
`BonusPointsForGuessTests`, matching how other Valkey-cache-touching test classes in this suite
already reset between tests.


- **`dev.urbanlens.org` sits behind Cloudflare, which caches everything under `/static/...`
  (compiled `style.css`, `map-annotations.js`, `article-wysiwyg.js`, etc.) for 4 hours
  (`Cache-Control: max-age=14400`), keyed on the exact URL - these files have no cache-busting
  hash in their filename, so a fresh rebuild+redeploy does NOT make the live site serve the new
  CSS/JS immediately; `curl -sD -` will show `cf-cache-status: HIT` and a `last-modified` older
  than the actual rebuild. A live browser check done shortly after deploying a CSS/JS-only change
  can silently verify the OLD stale asset while looking like a pass (structural/HTML changes are
  unaffected - Cloudflare doesn't cache the dynamic page response, only `/static/` files).
  **Workaround for verification**: fetch the asset with a cache-busting query string
  (`curl "https://dev.urbanlens.org/static/dashboard/style.css?cb=$(date +%s)"`) to force
  `cf-cache-status: MISS` and confirm what's actually at the origin, or use Chrome DevTools
  Protocol's `CSS.getMatchedStylesForNode` (via a Playwright CDP session) to see which rule a
  live page actually applied if a rendered value looks unexpectedly stale. (This applies to the
  public dev/staging/prod deployments, not the local docker-compose stack described above.)

## 2026-07-28: `test_loopnet.py::FetchTests::test_unconfigured_gateway_gracefully_persists_empty` makes a real outbound connection on this machine

Noted while running the full test suite as a regression check after adding `api_kinds =
frozenset()` to five plugins' `PanelSource` subclasses (an unrelated change - see Part 6 of
`docs/notes/mobile_app_notes.md`). This test's own docstring says it expects `RedataGateway()` to
raise `ValueError` because it's *unconfigured* in the test environment, and only mocks
`LocationCache.set`. On this machine it instead reaches all the way into `requests` and attempts a
real TCP connection to `10.2.0.214:443`, which the test sandbox's `LocalhostOnlyNetwork` guard
correctly blocks (`src/urbanlens/core/testing_network.py`) - so the symptom here is a hard failure
rather than the silent false-negative the test is nominally guarding against.

Root cause confirmed: this checkout's `.env` sets `UL_REDATA_API_URL=https://redata.urbanlens.org`
(a real, working REData instance, `resolve`d to `10.2.0.214` here) plus `UL_REDATA_API_KEY` -
legitimate for this developer's normal workflow against the real service, but Pydantic's
`app.py` settings load `.env` unconditionally, so it also configures `RedataGateway` under
`pytest`/`UL_ENVIRONMENT=test`. The test's own name ("unconfigured gateway") only holds on a
checkout with no REData credentials in `.env` at all; on this one it isn't unconfigured, so
`fetch()` proceeds past the `ValueError` branch straight into a real HTTP call, which the test
sandbox's `LocalhostOnlyNetwork` guard (`src/urbanlens/core/testing_network.py`) then correctly
blocks. Not fixed here - out of scope for the change that surfaced it (`git diff` confirms
`plugins/builtin/loopnet.py`'s only edit was the additive `api_kinds` class attribute, nowhere
near `fetch()` or `RedataGateway`) - but the test should mock/patch the gateway (or settings
should force REData unconfigured under `UL_ENVIRONMENT=test` the way `settings/_gdal_windows.py`
scopes its own local-only behavior) rather than depending on `.env` being REData-credential-free.

## Codebase audit (2026-08-06, module 1: core infrastructure) - findings & fixes

- **`ApiCallLog` grew forever** - `prune_older_than_days` was written and documented as
  the trim mechanism, but nothing ever called it, while every external API call runs three
  rate-limit COUNTs over the table plus an INSERT. Now pruned daily
  (`tasks.prune_api_call_logs`). Retention is 400 days, set by the table's *longest reader*:
  the public costs page reconstructs a 12-month API-spend chart from these rows, so the
  model helper's 90-day default would have silently zeroed three-quarters of that chart.
  If you add a longer-window consumer of ApiCallLog, raise the retention with it.
- **Slug retry loops retried on any unique violation** - `PublicDashboardModel.save()`/
  `regenerate_slug()` matched any "duplicate key" IntegrityError, so a violation of an
  unrelated constraint (e.g. Pin's one-pin-per-location-per-profile) burned 20 slug
  regenerations and could mask the real conflict behind a uuid-suffixed slug. Now only a
  violation naming a *slug* constraint retries; this makes "slug constraints have 'slug'
  in their name" a load-bearing naming rule (verified: db_pin_unique_slug_per_profile,
  uq_pin_list_profile_slug, uq_album_pin_slug/uq_album_wiki_slug, and the field-level
  uniques on Location/Profile all qualify).
- **Removed `abstract.Serializer`** - a custom DRF base with context-driven
  include/exclude-fields machinery that no serializer ever subclassed (every real one uses
  `serializers.ModelSerializer` directly). Unadopted abstraction; DRF's documented dynamic-
  fields recipe covers the need if it ever materializes.

Still open (bigger than this pass): `check_rate_limit`'s three COUNTs run per external
call; fine while windows are indexed and the table is pruned, but a hot service could
justify a cached counter. `EmailSendLog` is unbounded too - low volume (user-triggered
email), so left alone deliberately.

## Codebase audit (2026-08-06, module 2: pin/location/place) - findings & fixes

- **`PinQuerySet.overlapping()` was O(n²) intersects plus 3-5 queries per pin** - and it is
  user-reachable (the `overlapping_pins` saved-filter criterion), so a big collection turned
  each map filter application into thousands of queries. Now select_relates every relation
  `resolve_for_pin`'s fallback chain touches and sweeps sorted x-extents so only genuinely
  bbox-overlapping pairs run real geometry. Still open: `Boundary.resolve_for_pin` itself is
  per-pin (own row -> wiki row -> place polygon); a batched resolver using the existing
  `rows_by_pin_id` would cut the remaining per-pin queries but changes a subtle fallback
  chain - do it with care, with `map_pin_share_detection.py` as the pattern.
- **`WikiCreationService` lived in `services/locations/creation.py`** - wiki creation filed
  under locations, while every other wiki service lives in `services/wiki/`. Moved to
  `services/wiki/wiki_creation.py`; all imports/docs updated.
- **Stale TODO removed** - `Location.display_name` carried "TODO: assess for deletion" while
  being load-bearing in tasks, comments, and the map controller.

Noted, deliberately not changed: `Location.__setattr__` special-cases a duck-typed
`google_place` for unit tests - test scaffolding in production code; removing it means
fixing several tests to build real GooglePlace rows. `enrichment.py`/`external_data.py`
carry seven "TODO: Catch specific exceptions" blanket handlers - each is a deliberate
"never let one source break the cycle" guard, so narrowing them is safe only with
per-source failure tests.

## Codebase audit (2026-08-06, module 3: tasks/consumers/celery) - findings & fixes

- **Redis visibility_timeout was the default 3600s with acks_late on** - exactly equal to
  both the hard CELERY_TASK_TIME_LIMIT and the longest countdown= this app schedules
  (import/export cleanup 3600s, check-in archival grace 1h). At that boundary a
  legitimately long task, or a countdown sitting unacked in a worker, is redelivered and
  runs twice. Now pinned to 2h via CELERY_BROKER_TRANSPORT_OPTIONS; keep it above
  max(time_limit, longest countdown) if either grows.
- **Two raw `.delay()` calls bypassed `safely_enqueue_task`** (DM address-mention
  detection inside an on_commit callback, and fact-confidence recompute after evidence
  recording). Both fire after their row is durably saved, so a broker outage turned an
  already-successful write into a 500. Routed through the guarded helper; the facts test
  that asserted on `.delay` re-pointed to the enqueue seam.

Verified clean elsewhere: every signal connect carries dispatch_uid; every
`.objects.get(pk=)` in tasks.py is DoesNotExist-guarded; no @shared_task lives outside
tasks.py; consumers never touch the ORM outside database_sync_to_async; the beat schedule
references no missing task, and no task is unreferenced. Queue split (default "celery" +
"panel_fetch") matches the two workers' -Q flags.

Also: the pre-audit full suite completed 10018 passed / 1 failed, the one failure being
the facts test above mid-run (the run's snapshot was taken before that fix) - i.e. the
tree entering this audit was fully green.

## Codebase audit (2026-08-06, module 4: plugins/API gateways) - findings & fixes

- **Digital Commonwealth was registered everywhere except where it mattered** - the
  rate limiter and site-admin category map both knew the service, REData has fronted it
  all along, and a direct-API `DigitalCommonwealthGateway` sat fully dead (never called by
  any plugin) - so Massachusetts pins simply lacked the archive. Wired the missing half:
  `DigitalCommonwealthMediaProvider` (REData `reference-documents/search/`, MA-gated,
  same query-shape flags as its LOC/IA siblings), a `DigitalCommonwealthPlugin`, gallery
  loaders on both pin and wiki pages, and an `ImageSource` value so wiki-sends keep
  attribution. Deleted the dead direct gateway. 43 plugins now discovered.
- **The admin "Search provider" picker was a dead control** - web search moved wholesale
  to REData's own provider chain, so `SiteSettings.search_provider` was written by the
  admin form and read by nothing; an admin changing it changed nothing. Removed the
  picker, the setting (migration 0037), `SearchProviderChoice`, and the never-read
  `mojeek_api_key`/`marginalia_api_key` env settings from the same era.
- **Orphan rate-limit registry entries removed** - `google_search` and `news` had no
  gateway, plugin, or caller anywhere; they only rendered as permanently-zero rows in the
  admin API-usage report.
- **Removed `StaticBoundaryProvider`** - its docstring said "kept for tests and explicit
  callers"; there were neither.

## Codebase audit (2026-08-06, module 5: media/images pipeline) - findings & fixes

- **Eight redundant uuid indexes dropped** (migration 0038) - Pin, Image, Wiki, Trip,
  Label, Comment, PinVisit, SafetyCheckin each declared `Index(fields=["uuid"])` while
  `FrontendDashboardModel.uuid` is already `unique=True`, and a unique constraint *is* a
  btree index in Postgres. Every insert/update on some of the hottest tables maintained a
  second identical index for nothing. If you add a new FrontendDashboardModel, don't
  re-add one.
- **Map-overlay uploads bypassed the upload pipeline** - the overlay controller (added
  earlier this week) did a raw `Image.objects.create`, skipping the quota check and its
  per-profile lock, checksum dedupe, `file_size` (so an overlay sheet never counted
  against quota), and the async EXIF/keyword ingestion. Now goes through the canonical
  `services.photos.photo_upload.upload_photo`. Anything creating an Image from a *user
  upload* must use that service; raw creates are for server-fetched bytes only
  (materialize sets its own size/checksum).

Verified clean: visible_to's per-uploader relationship checks are bounded by the
gallery's distinct uploaders (documented tradeoff); every other Image-creating path sets
file_size; media_source_key/media_item_key lookups are indexed; storage sums use the
single-pass filtered-aggregate helper.

## Codebase audit (2026-08-06, module 6: wiki/community) - findings & fixes

- **Wiki pages had no way up to their parent** - wikis nest themselves (a building's wiki
  becomes a child of the campus's, a documented feature), and `wiki_access.visible_parent_wiki`
  was written specifically to make that link safe, but nothing ever called it: no template
  rendered a parent breadcrumb, so a viewer landing on a nested building wiki was stuck
  there. Wired it into the wiki hero beside "Back to my pin".

  The gating is the point and is now covered by tests: within one access domain (a building
  `PART_OF` its parcel) the parent is always reachable, but across a `MEMBER_OF` edge - a
  campus earned only by holding *every* member parcel - the parent's **name must not reach
  the response at all**, since a breadcrumb to a page that would 404 confirms a place the
  viewer hasn't earned exists. The new test asserts absence from the response body, not just
  absence of a link.

Verified clean: every wiki-scoped controller and external-API view resolves through
`resolve_visible_wiki` (the two `location_slug` users that don't - `maps.py` building a URL,
`pin_edit.py` relinking a pin to a Location - aren't wiki access). No dead functions in
services/wiki, services/comments, or services/consensus once helpers used within their own
module are accounted for.

Left alone deliberately: `place_visible_to` is public but referenced only by tests - it is a
coherent part of the access module's public surface (the place-level twin of
`location_visible_to`), so it reads as API completeness rather than dead weight.

## Codebase audit (2026-08-06, module 7: messaging/e2ee/social) - findings & fixes

- **Notification dropdown was a silent N+1** - `notification_item.html` reads the
  `pin_share`/`visit_suggestion` reverse OneToOnes to decide whether to offer Accept/Decline
  (a shared pin) or the three-way merge choice (a suggested visit), but all six call sites
  selected only `source_profile`. Twenty rows therefore cost up to forty extra queries on
  nearly every page load. The miss is *invisible* by construction: an absent reverse
  OneToOne raises `ObjectDoesNotExist`, which Django templates swallow - so nothing breaks,
  it just gets slow. Added `NotificationQuerySet.for_display()` and routed every
  dropdown-rendering call site through it, so the relation set lives in one place as the
  template grows. `notification_center` (external API) deliberately keeps its narrower
  select: its serializer never reads those relations.

  Worth recording how this was nearly mis-diagnosed: `NotificationLog` has no `pin_share`
  field, so a field-list search says the template references something that doesn't exist,
  which reads as "these buttons never render". They do - the names are the `related_name`s
  of OneToOneFields declared on `PinShare`/`VisitSuggestion`. A reverse accessor is easy to
  miss from the model's own side; check both directions before concluding a template branch
  is dead.

Verified clean: every `PinShare.objects.create` path (five of them - direct share, re-share,
DM address detection, map share, trip share) resolves lineage and calls
`record_share_exposure`, so the `LocationExposure` chain CLAUDE.md calls out is intact.
Native push is fully wired (notification post_save -> on_commit -> `dispatch_native_push`).
Comment mention-gating holds on every surface, including the external API, which serializes
raw text only for comments that already passed the `VisibleComment` gate. No dead functions
in services/messaging, services/social, services/sharing, or services/notifications.

## Codebase audit (2026-08-06, module 8: auth/billing/subscriptions/security) - findings & fixes

- **A redelivered Stripe payment could be credited twice (money bug).** `invoice.payment_succeeded`
  is the one handler with a non-idempotent side effect: `banking.apply_payment` *increments*
  `total_paid_cents`, which drives `granting_access_for` (i.e. how long pay-what-you-want access
  stays granted). `StripeWebhookView` recorded the event, handled it, then marked it processed -
  three separate autocommits. Two ways that double-credits, both of which Stripe's retry policy
  makes routine rather than theoretical (it redelivers on any non-2xx **or timeout**):
  a delivery that applied the credit but died before writing `processed_at` gets retried and
  credits again; and two deliveries arriving at once both read `processed_at` as null and both
  run the side effects. Handling and marking-as-handled now commit together inside one
  `transaction.atomic()`, entered after re-reading the event row `select_for_update()` so
  concurrent duplicates of the same event serialise on it. The audit row is still inserted in its
  own prior transaction, so a delivery whose handler raises leaves its raw payload behind to debug
  from. Regression tests cover both paths, including a handler-succeeds-then-marking-fails
  delivery followed by a retry.

- **TOTP replay protection was a read-modify-write.** `verify_totp_code` read `last_used_step`,
  compared, then wrote the new step unconditionally - so two submissions of one intercepted code
  (a phishing proxy replaying it into a parallel session) could both pass the comparison before
  either wrote, which is the exact thing tracking the step exists to stop. The step is now claimed
  in the same statement that checks it (`filter(last_used_step__lt=step).update(...)`, verdict from
  the matched-row count), so Postgres serialises it and exactly one caller wins.

- **The SiteSettings singleton was re-fetched several times per page.** `get_current()` is called
  from ~80 places, and each was its own `get_or_create(pk=1)` round-trip: three separate context
  processors fetch it on every request, then the controller fetches it again, then every
  `user_has_feature()` check fetches it once more. Added a request-scoped memo
  (`models/site_settings/request_cache.py`), armed by `request_started` and torn down by
  `request_finished`. Deliberately **not** a process-wide or TTL cache: long-lived Celery workers
  would pin a settings row for the life of the worker, and the test suite mutates settings through
  `queryset.update()`, which bypasses `save()` and so cannot invalidate anything. Anywhere without
  a request never arms the memo and reads through exactly as before. `post_save` clears it so an
  admin editing settings sees their own change for the rest of that request.

Verified clean: the Stripe webhook does verify signatures (`stripe.Webhook.construct_event` against
`UL_STRIPE_WEBHOOK_SECRET`, 503 when unset) and is the codebase's only CSRF-exempt endpoint, for a
documented reason. All 22 API scopes are enforced by at least one view; `ExternalApiView`'s
fail-closed default genuinely holds (`credential_grants` returns False for a null credential or an
empty requirement, and `OAUTH2_ONLY_SCOPES` is refused to PAT-style keys regardless of the stored
grant); `UnscopedExternalApiView` is a documented, deliberate exception. API-key auth hashes with
Django's password hashers and looks up by indexed public prefix, so it neither iterates every hash
nor leaks by timing. Login 2FA is lockout-rate-limited. `services/billing/{banking,pricing,
stripe_client}` and `services/security/{redact,malware_scan}` are all wired, with no dead functions
in `services/auth`, `services/billing`, or `services/security`.

Not a gap, though it looks like one: `controllers/comments.py` passes `skip_malware_scan=True`.
The scan is deferred to a Celery task instead, with the comment held `pending_scan` (hidden from
every other viewer) until it clears - a slow/occasionally-unavailable clamd used to fail whole
comment submissions.

## Codebase audit (2026-08-06, module 9: games, trips, safety, memories) - findings & fixes

- **One bad safety check-in could silently suppress everyone else's escalation.** The three
  safety beat tasks (`send_due_checkin_reminders`, `send_final_checkin_warnings`,
  `escalate_overdue_checkins`) each looped a queryset calling a per-check-in service with no
  per-item guard, so any exception aborted the whole run. `SafetyCheckin` has a deterministic
  `ordering`, so a row that fails repeatably - corrupt contact data, an address the mail backend
  rejects, a template that won't render - fails at the same position on *every* tick, and every
  check-in behind it never escalates. The failure is silent and unbounded: the sweep just returns
  early, and the people whose emergency contacts were never called have no way to know. This is
  the most consequential loop in the app to leave unguarded, and the fix was already the
  file's own convention - `sweep_due_safety_checkin_archival` immediately below, and all four
  game/trivia/consensus stall sweeps, already isolate per item. These three were simply missed.
  `escalate_checkin` is per-contact idempotent (`notified_at__isnull=True`), so retrying a failed
  check-in on the next tick reaches only the contacts the failed attempt never got to.

- **Same shape in two billing sweeps**, fixed while here. `advance_pwyw_usage_ledgers` had no
  guard at all - and it is the only thing counting a canceled subscription's banked balance down,
  so one bad row froze every other user's ledger. `sync_stripe_subscriptions` guarded the Stripe
  *fetch* but not the *apply*, and `sync_from_stripe_subscription` indexes `items.data[0]`, so a
  subscription in an unexpected shape aborted the sweep for everyone after it.

- **The Memories feed re-queried up to three times per trip.** `_trips_for_range` annotated
  `_eff_start`/`_eff_end` to filter on, then discarded them and re-derived the same two dates
  through `Trip.effective_start_date`/`effective_end_date` (a query apiece for any trip without
  explicit dates), then called `_trip_representative_point`, which ran a third. Now the
  annotations are used directly and activities arrive via a `Prefetch` carrying the same
  `select_related`/ordering the point helper wants. Pinned with a test asserting the query count
  is flat from one trip to six.

- **The same code filtered and displayed trips by different definitions of "ends".** That
  annotation took `Max(activities__scheduled_at)` while `effective_end_date` takes the later of
  `scheduled_at` and `scheduled_end`. A trip whose final activity *runs past* the last start time
  fell in the gap: filtered out of a window it visibly overlaps. The annotation now uses
  `Greatest(Max(scheduled_at), Max(scheduled_end))` (Postgres's `GREATEST` ignores NULLs, so an
  activity with no end doesn't erase the max), so the two agree by construction.

- **Wired up `trip_name_suggestions`**, written for the create-trip dialog's placeholder rotation
  and never called. The dialog carried a hand-maintained 12-name JS array instead - a second list
  that would drift from the generator that actually names blank-submitted trips. Now exposed
  through a `trip_name_ideas` template tag (matching the existing `subscription_role_choices`
  precedent, since the dialog is included from two pages) and handed to JS via `json_script`.

Verified clean: a definition-level sweep for unwired public functions across all four subsystems
came back empty apart from the above - calendar sync, trip AI suggestions, and the trip signal
receivers are all properly connected (`@receiver` plus the `apps.py` import). Games access control
is systematic: every session-scoped SpotGuessr view resolves through `_participant_session`, which
404s rather than 403s so a non-participant can't confirm a session id exists, and host-only actions
(`begin_session`, `end_session_now`) check `host_profile_id` in the service layer rather than the
view. The memories aggregator's other three sources already `select_related` correctly.

Note on method: an early dead-code sweep that excluded each function's whole defining file produced
17 false positives (`clamp_rounds`, `can_delete_comment`, `invite_members` and more are all called
by their own module's public entry points). Excluding only the `def` line itself cut it to three.

## Codebase audit (2026-08-06, module 10: frontend TS, templates, SCSS) - findings & fixes

- **A multi-line `{# #}` comment was rendering to users on the wiki page.** Django's lexer matches
  comments with a non-DOTALL `{#.*?#}`, so a `{#` whose `#}` sits on a later line is never
  tokenised as a comment at all - it falls through as ordinary text and prints verbatim in the
  middle of the page. `pages/location/wiki.html` had one, three lines of internal notes about
  pin-count truncation, rendered right under the "First pinned" stat. Nothing fails loudly: the
  page still returns 200 and the markup around it is fine, which is exactly why it survived.
  Converted to `{% comment %}`, and added `test_template_comment_lint.py` - a `SimpleTestCase`
  (no DB, runs in under two seconds) that scans all 416 templates for the same shape, so the
  rule CLAUDE.md already states is now enforced rather than remembered.

- **The pin cache's version and key were hardcoded on both sides of a cross-language contract.**
  `pages/map/index.html`'s inline script is the only writer; `pin-cache.ts` is the reader; the
  version (`v: 8`, `c.v !== 8`) and the key (`ul_pins_v5_${uuid}`) were spelled out independently
  in each. A one-sided bump makes every read return `[]` - no error, the features built on it just
  go quiet. This is not hypothetical: the module's own comment records that it already happened
  (the reader sat on v6 after the writer moved on). Exported `PIN_CACHE_VERSION`/`pinCacheKey`
  and added `pin-cache.contract.test.ts`, which parses the template and fails when the two sides
  disagree. Confirmed it genuinely fails on drift by bumping one side before committing.

- **Deleted `shared/parent-search.ts`** - dead on three independent counts: no entrypoint imports
  it (so it is never bundled and the `window.filterParentOptions` global it installs never
  exists), no template calls that global, and the `.tag-parent-option` class it queries appears
  nowhere but inside the file itself. It is a superseded client-side filter; the live UI does
  server-side search against `/map/pins/parent-search/`.

Verified clean: a reachability walk from the 14 real entrypoints found only that one orphan plus
`tools/generate-e2ee-fixture.ts` (a dev tool, legitimately run by hand). Every `|add:` on an id in
all 416 templates already goes through `|stringformat:"s"` - the documented `''`-collapse gotcha
has been fixed thoroughly. No orphaned SCSS partials. No other template/JS data duplication of the
kind the trip-name array was: every hardcoded JS array in a template is an empty accumulator, and
the one enum-shaped list (`_TRIP_TABS`) is a set of DOM tab names, not a mirror of a server enum.
`tsc --noEmit` is clean and all 150 bun tests pass.

Pre-existing, not fixed here: `bun run build` fails at `Formats besides 'esm' are not implemented`
when it reaches the `entries-classic` (iife) step, with the installed Bun. Confirmed identical at
HEAD, so it is not a regression from this work. Note it partially writes output before failing -
it deleted `core.js`/`e2ee.js`/`permissions.js`/`webauthn.js` from the tracked static output and
rewrote the esm bundles. Anyone running it needs to `git checkout` the static js directory
afterwards; the tracked build artifacts should not be committed from a run that aborts halfway.

## Codebase audit (2026-08-06, module 11: settings, urls, labels/saved-filter/search/import-export) - findings & fixes

- **`UL_EMAIL_TLS=true` silently disabled STARTTLS.** `base.py` parsed it as
  `os.getenv("UL_EMAIL_TLS", "True") == "True"` - a literal string comparison, so every spelling
  but exactly `True` evaluated to False. The same variable is *also* declared in `app.py` as a
  pydantic `bool`, which accepts `true`/`1`/`yes`/`on`, so the two readers of one variable
  disagreed - and the disagreement resolved toward sending SMTP credentials and every outbound
  mail in plaintext, while the operator's env file plainly said TLS was on. Nothing surfaces
  that: mail still sends. `.env-sample` ships `True`, which is why it went unnoticed. Added
  `settings/_env.py:env_bool()` (accepts the spellings people actually write, and falls back to
  the *default* rather than False on an unrecognised value, since False is the unsafe direction
  here) and routed both `EMAIL_USE_TLS` and `EMAIL_USE_SSL` through it.

- **Removed three env vars from `.env-sample` that no code reads**: `UL_DPLA_API_KEY`,
  `UL_US_CENSUS_API_KEY`, and `UL_CLOUDFLARE_WORKER_AI_API_KEY`. An operator setting these gets
  silence - the census gateway (`census_tigerweb`) is keyless, DPLA is an unbuilt roadmap
  candidate (it stays documented in `docs/ROADMAP.md`, which is where a not-yet-built integration
  belongs), and the Cloudflare one is a near-miss for the real `UL_CLOUDFLARE_AI_API_KEY` /
  `UL_CLOUDFLARE_WORKER_AI_ENDPOINT` pair, both of which do exist and are untouched.

- **Two URL patterns share the name `404`** (`UrbanLens/urls.py:113` and
  `dashboard/urls.py:1997`), both in the root namespace, so `reverse("404")` is ambiguous and
  resolves to whichever registered last. Left as-is deliberately: nothing reverses that name, and
  both catch-alls are correctly *scoped* (the dashboard one only sees `/dashboard/*`, since the
  app is included under that prefix), so this is a naming wart rather than a routing bug. Noted
  here so the next person who greps for it doesn't re-derive the analysis.

- **`admin_username` / `admin_email` settings are read by nothing.** They export as
  `ADMIN_USERNAME`/`ADMIN_EMAIL`, which are not Django settings and which no code consults. Left
  in place (removing pydantic fields is a crash-loop risk class per CLAUDE.local.md, for no
  functional gain) but recorded as dead configuration.

Verified clean: of 89 pydantic settings fields, only those two are genuinely unread - the other 14
that a naive grep flags are consumed through `app.py`'s uppercase auto-export into Django settings
(`ROOT_URLCONF`, `TIME_ZONE`, `STATIC_URL`, ...), which a lowercase search misses. Route names are
otherwise collision-free across namespaces (the only other same-namespace duplicates are DRF's own
format-suffix pairs). No dead code in `models/labels`, `models/saved_filter`, `services/search`, or
`services/import_export` - the four label signal receivers a definition-level sweep flags are all
`@receiver`-decorated with `dispatch_uid` and connected on import. The export path is thoroughly
prefetched: every `.all()` iteration over an M2M has a matching `prefetch_related` on the queryset
feeding it. The import path bounds decompression against zip bombs. `saved_filter_cache` keys on a
fingerprint of `updated` timestamps, so entries self-invalidate rather than needing explicit
invalidation hooks.

Corrected mid-audit: I initially read `_queryset_for_kind` (labels controller) as an N+1, since it
calls `.with_pin_counts()` where the organize page calls `.with_hierarchy()`, and the shared card
template reads `label.parents.all`. It is not - `with_pin_counts()` prefetches `parents` and
`children` itself. Worth recording because the shape (two call sites, one prefetch helper) is
exactly the module 7 notification bug, and here it turned out fine.

---

## Codebase audit (2026-08-06): consolidated summary of modules 1-11

Eleven modules, one pass each, module-by-module across the whole codebase. 26 distinct defects
fixed. Each module's own section above has the detail; this is the shape of what was found.

**The four that would have hurt most in production**, all silent-failure bugs - the system keeps
returning 200s and the damage is invisible until someone goes looking:

1. *One bad safety check-in could suppress everyone else's escalation* (module 9). Three beat
   tasks looped without per-item guards over a deterministically-ordered queryset, so a single
   repeatably-failing row starved every check-in behind it, on every tick, forever. Emergency
   contacts silently never called.
2. *A redelivered Stripe payment could be credited twice* (module 8). Handling and
   marking-as-handled were separate commits, and the credit is an increment. Stripe retries on
   timeouts, so this was routine, not theoretical.
3. *`UL_EMAIL_TLS=true` silently disabled STARTTLS* (module 11). A literal `== "True"` compare
   against a variable that pydantic elsewhere parses as a bool - SMTP credentials and all mail in
   plaintext while the env file said otherwise.
4. *Redis `visibility_timeout` left at 3600s with `acks_late` on* (module 3). Any task running
   over an hour gets redelivered and runs twice, concurrently.

**Recurring patterns, worth internalising more than any single fix:**

- *Two call sites, one fix.* Repeatedly a helper existed and was used correctly in one place and
  missed in another: notification prefetch (7), safety sweep isolation (9, where the file's own
  archival sweep three functions below already did it right), billing sweeps (9). When fixing a
  N+1 or adding a guard, grep for every caller of the same template/service - the second one is
  usually there.
- *Read-modify-write where the DB could do it atomically.* TOTP replay steps, webhook
  idempotency, storage quota, `get_nearby_or_create` - each a check-then-act that two concurrent
  requests both pass.
- *Duplicated constants across a language boundary.* The pin-cache version (10), the trip-name
  list (9), `EMAIL_TLS` vs `EMAIL_USE_TLS` (11). None of these can be caught by types; they need
  a test that reads both sides, which is now what guards the pin cache.
- *Silent-by-construction failures.* An absent reverse OneToOne that Django templates swallow
  (7), a cache-version mismatch that just returns `[]` (10), a sweep that returns early (9). The
  common thread: no exception, no log, no user-visible error - only a test asserting the
  *positive* behaviour catches them.

**On method.** Two things repeatedly produced false leads and are worth flagging for whoever
audits next. First, definition-level "dead code" sweeps: excluding a function's whole defining
file yielded 17 false positives in module 9 alone (functions called by their own module's public
entry points), and `@receiver`-decorated signal handlers look unreferenced no matter how you grep.
Every "dead" finding here was confirmed by hand before deletion, and three suspected N+1s and one
suspected dead template branch turned out to be fine on inspection - those are recorded too, since
a wrong conclusion re-derived later costs as much as the original bug. Second, this environment's
test suite: running against the dev compose stack trips the localhost-only network guard for every
page-rendering test, so raw failure counts are meaningless. Every module's suspicious failures
were re-run against a HEAD baseline before being attributed; in each case (166, 88, 19) the
baseline was identical and the changes were clean.

**Known-broken, not fixed here:** `bun run build` fails at its `entries-classic` (iife) step with
the installed Bun, and deletes tracked static output before failing (module 10).

## Second pass (2026-08-06): concurrency & data-integrity races - findings & fixes

The first pass kept turning up one shape - check-then-act that two concurrent callers both pass
(TOTP replay steps, the Stripe webhook marker, storage quota, `get_nearby_or_create`). This pass
went looking for the ones it missed, prioritising money, quotas, access grants and safety.

- **Consensus tentative answers were accumulated without a lock.** `record_tentative_answers` is
  find-then-bump-or-create, and two rounds resolving at once for the same wiki is ordinary, not
  exotic - separate sessions play the same popular wiki concurrently. A threaded test reproduces
  it exactly: both reads miss, both insert, and one dies with
  `IntegrityError: duplicate key value violates unique constraint "db_consensus_tentative_unique"`,
  taking round resolution down with it and losing the players' answers. No unique constraint can
  cover this on its own - the coordinate branch dedups by *proximity*, which no constraint can
  express, so there the same race is silent instead: two rows for one location with support split
  across them, meaning a value the community actually agreed on never reaches the promotion
  threshold. That is the feature failing at its whole purpose, quietly. Fixed by locking the
  parent wiki for the upsert, which covers both branches, makes the row-level `+=` safe, and only
  ever contends with another resolution for the same wiki.

- **E2EE key reset applied rewraps computed against a superseded key.** The endpoint read the
  bundle, had the client rewrap every conversation and group envelope against *that* public key,
  and only then opened its transaction - the docstring's "one atomic transaction, no partial
  state" was true of the writes but not of the read they depended on. A second reset landing in
  between (a double-submitted or retried request - the endpoint is slow, so this is the realistic
  path) meant the second request applied its rewraps on top and overwrote the version. Confirmed
  at HEAD: the test drives a competing bump to v6 mid-flight and the request still returns 200,
  logging `now v2` - it clobbers the newer bundle and seals conversations to a key the bundle no
  longer advertises. Those threads are then undecryptable, and there is no recovery path. Fixed
  by re-reading the bundle `select_for_update()` inside the transaction and returning 409 if the
  version moved; the client restarts against the current key. The request has to lose cleanly
  rather than half-succeed.

Verified clean: `services/consensus/points.py` already locks its `ConsensusProfile` row correctly,
and the trivia/consensus/spotguessr round-completion checks all operate on a `locked_round`. The
E2EE *enrollment* check-then-act (`exists()` then `create()`) is backstopped by `profile` being a
`OneToOneField`, so a concurrent double-enroll fails loudly with an IntegrityError rather than
producing two bundles.

Recorded, not fixed: a family of count-then-create limit checks (`max_partners`, `max_upcoming`
trips, `max_members`, `max_activities`) can each be exceeded by one or two under concurrent
requests. Real, but bounded and self-correcting - a user ends up slightly over a soft cap, with no
corruption and nothing unrecoverable - so they are noted here rather than each acquiring a lock.

Method note: this environment cannot run most of the affected suites - the Redis network guard
fails every test whose request path touches the cache, including the *entire* class the E2EE reset
tests live in (36 of 47 fail at HEAD). Both fixes here were verified by pinning that one test's
cache to locmem and running the RED/GREEN pair explicitly against HEAD and against the fix, rather
than by reading a green summary line.

## Second pass (2026-08-06): cross-module interactions - findings & fixes

Looking for bugs that live *between* modules, which a module-by-module pass structurally cannot
see: each module looks correct in isolation and the defect is in the seam.

- **Deleting a pin from its detail page left a ghost marker on the map.** Three correct-looking
  pieces compose into a bug. (1) The map caches pins in localStorage. (2) Its background poll
  decides whether to refresh by comparing `Max(updated)` across the profile's pins - which a
  *deletion* cannot advance, so the poll is structurally blind to deletions; the only signal is
  the `ul_pins_dirty` flag. (3) `deletePinCascade` is a shared helper in `base.html` called from
  both the map page and the Private Pin page, and it did not set that flag - the map's call site
  set it separately, and the detail page's call site did not. So a pin deleted from its detail
  page stayed in the map's cache indefinitely, restored on every subsequent load, with a marker
  that 404s when clicked. It only self-corrected in the one case where the deleted pin happened
  to be the most recently updated one, which changes `Max(updated)` and trips the poll's
  not-equal check by accident.

  Fixed in the shared helper rather than at the second call site, so every present and future
  caller inherits it. Guarded by a contract test that brace-matches the helper's body out of the
  template and asserts the flag is set on the success path (and not before the user-cancelled
  bail-outs) - the same approach used for the pin-cache version contract, and for the same reason:
  the invariant spans a template and its callers, where no type or constraint can reach.

  Checked the neighbours: map bulk-delete is map-only and manages the cache directly, and
  undo-restore recreates the row with a fresh `updated`, which the poll does catch. Deletion was
  the unique blind spot.

Verified clean: every `@receiver` in the codebase passes `dispatch_uid`. No Celery task is
enqueued inside a `transaction.atomic()` block without `on_commit` (checked by AST, not grep). No
`@shared_task` calls `.objects.get()` unguarded, so a task dispatched by one module tolerates
another module deleting its subject first. All six user-upload paths that create an `Image`
(gallery x2, safety, consensus, direct messages, and the shared photo-upload service) run the
`image_upload_error` gauntlet. All six `PinShare.objects.create` paths pair with
`resolve_and_stamp_origin_share`/`resolve_origin_share` + `record_share_exposure`, so the
`LocationExposure` provenance chain CLAUDE.md calls out is intact. Every render of
`notification_item.html`, including the single-item re-render after marking one read, goes through
`NotificationQuerySet.for_display()`.

## Second pass (2026-08-06): access control - findings & fixes

- **Relinking a pin was a way to *earn* wiki access rather than discover it.** Wiki visibility is
  deliberately gated on discovery - `location_visible_to` grants on an exact `Location` match - so
  which Location a pin points at is not a neutral preference, it is the thing that confers access.
  `PinRelinkView` scoped the *pin* to the requester (`_pin_for_user`) but resolved the target
  `Location` straight from the URL slug with no visibility check. And a Location's slug is its
  `official_name` (falling back to uuid only when unnamed), so the slug of any notable place is
  guessable: the test's undiscovered location slugs to literally `hudson-river-state-hospital`.
  Any authenticated user could therefore POST `pin/<their-pin>/link/<guessed-slug>/` and read the
  community wiki for a place they had never found - defeating the discovery rule CLAUDE.md calls
  out as deliberate design. Confirmed end to end against current code: before the fix the wiki
  returns 404, the relink succeeds, and the same wiki then returns 200.

  Fixed by requiring `location_visible_to(location, pin.profile)` before relinking. This breaks no
  legitimate flow, which is why the gate is safe rather than merely strict: the location picker
  offers the pin's own location plus `competing_wiki_locations`, which already filters to
  `accessible_domain_ids`, and the wiki page's switch button offers those same candidates - every
  reachable target already passes.

Verified clean, mechanically rather than by eye: an AST sweep for `get_object_or_404` on a
request-supplied id with no scoping kwarg and no pre-filtered queryset found 59 call sites, and
every one either re-checks ownership on the next line (`image.profile_id != profile.pk`,
`suggestion.profile_id != profile.pk`), gates through a service (`is_accepted_partner`,
`is_owner_or_accepted_partner`), is a token credential, or is admin-only. `detail_pins.py` resolves
a Location from a slug but gates it with `location_visible_to` immediately. All three
wiki-scoped `Image` queries apply `.visible_to(profile)`. `views_wiki.py` consistently re-scopes
children to the resolved wiki (`get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)`). The wiki
access helpers are fail-closed on a missing place or domain root.

### Environment: the dev container was 30 tracked files behind the working tree

Found while chasing a `ModuleNotFoundError` for `services.places`: the `app` container image was
built from an older commit and was missing 30 tracked files - the whole `models/place`,
`models/album` and `models/map_overlay` packages, several controllers, and migrations 0026-0038.
Every `docker exec ... pytest` run in this audit before this point therefore executed against a
materially older codebase than the working tree.

The HEAD-vs-fix baselines stand (both sides ran in the same stale container, so the comparison was
like-for-like, and that is what the no-regression conclusions rest on), but any absolute
"the suite passes" reading of those runs was weaker than it looked. `docker cp src/urbanlens/.
urbanlens_devs1_app:/app/src/urbanlens/` resyncs it; the pin-relink fix above was re-verified both
ways *after* syncing. Worth checking the container is current before trusting a test run here.

## Re-verification against the synced container (2026-08-06)

Every `docker exec ... pytest` run in this audit before the resync executed against an image 30
tracked files behind the working tree. Re-ran everything that matters now that `/app/src` matches.

- **All 36 regression tests this audit added still pass** against current code with the full
  migration set (0026-0038 present, so a fresh test database). The bun suite is green at 152/152
  and `tsc --noEmit` is clean. No fix in modules 1-11 or the second pass depended on the stale
  schema.

- **The container also held 46 files that no longer exist in the repo**, since `docker cp` adds but
  never deletes. Five were deleted plugin modules under `plugins/builtin/` - and
  `plugin_registry.discover()` scans that directory, so the container was registering
  `duckduckgo`, `marginalia`, `mojeek`, `searxng` and `opentopomap` as live plugins after module 4
  removed them. Deleted them so discovery matches the codebase. No stale *migrations* were present,
  so the migration graph was never wrong.

- **Correction to the pin-relink fix (a regression I introduced and caught here).** The first
  version gated relinking on `location_visible_to` alone. That was too strict: `PinRelinkViewTests`
  - six pre-existing tests I could not run before the resync - relink to a bare `Location` with no
  `Place` row, which that predicate rejects. Since the Places feature is recent (migrations
  0026-0028) and `Location.place` is nullable, a place-less target is an ordinary production case,
  not a test artifact, so the strict gate would have broken real relinking.

  The gate now accepts a target two ways, matching what the UI actually offers: one the profile can
  already reach, *or* one covering the pin's own coordinate (`get_all_for_point`, which falls back
  to a 50 m proximity check for place-less coordinates). A place the user's own pin sits inside is
  one they discovered by pinning it, so allowing it discloses nothing they could not derive - while
  a Location kilometres away with no relationship, which is the attack, is still refused.

  Four of those six tests placed their target kilometres from the pin, which quietly asserted that
  relinking to an arbitrary Location was allowed - the hole itself, encoded as a contract. Moved
  them inside the pin's own domain, preserving exactly what each test verifies (uuid-slug fallback,
  merge-vs-relink, another profile's pin not triggering a merge), and documented on the class why
  the coordinates are deliberate. All six pass with the corrected gate, as do the five
  pin-relink-access security tests.

Worth stating plainly: the too-strict first version passed every test I could run at the time. It
was caught only by reading the fixtures of tests the environment could not execute. When a test
cannot run, its assertions still encode a contract - read them.

## Recheck of the place/album/overlay suites (2026-08-06)

- **All 34 failures were the test suite's own cache configuration, not defects.**
  `settings/test.py` inherited `base.py`'s Redis/valkey-backed `CACHES`, so any test whose request
  path touched the cache depended on a live external service. Under the suite's localhost-only
  network guard that fails with an opaque "External network access is disabled during tests" -
  a message about the environment that says nothing about the code. Pointing the test cache at
  locmem turned the nine suites from 34 failed / 125 passed into **159 passed**, with no other
  change. That covers module 5's map-overlay upload fix and the albums work, whose models were
  absent from the container when those modules were audited and had therefore never actually been
  exercised; both are now genuinely verified.

  This was worth fixing at the source rather than per test class. It is also correct on its own
  merits: tests previously shared one cache instance, so entries could bleed between them, and
  locmem is per-process and needs nothing running.

- **The container held 10 orphaned test files** for modules this audit deleted earlier -
  `test_abstract_serializer` (module 1 removed `abstract.Serializer`), the five search-provider
  suites (module 4), and the Smithsonian/Internet Archive/NPS gateway suites (migrated to REData).
  None are tracked in the repo; they were leftovers from the old image, and they aborted full-suite
  collection with import errors for modules that no longer exist. Removed, so the container's tree
  now matches the repo exactly.

The through-line of this whole re-verification: for most of this audit, "the suite fails here" was
treated as an immovable property of the environment and worked around one test at a time. It was a
one-line defect in the test settings. Time spent early on making the harness trustworthy would have
paid for itself many times over - every module's verification was weaker than it needed to be, and
one fix (the pin-relink gate) shipped a regression that a runnable suite would have caught
immediately.

## Full-suite baseline (2026-08-06)

First complete run of the test suite in this audit, possible only after pointing the test cache at
locmem. **18 failed, 10,025 passed, 1,428 subtests passed, in 58 minutes** (`pytest src/urbanlens`,
container synced to the repo, fresh test database). That is a 99.8% pass rate and the reference
point future work should measure against - previously there was no number at all, only per-file
runs whose failures were indistinguishable from environment noise.

Triage of all 18, none caused by this audit's changes:

- **12 panel failures** (`test_panel_feature_gate` x3, `test_pin_panel_info` x7,
  `test_pin_panel_live_refresh` x2) - **pre-existing, genuine, and worth a dedicated
  investigation.** They fail with `204 != 200`: the tests seed `LocationCache` for a panel source
  and expect the panel to render, but the view falls through to its pending-loader path and then
  returns 204 from `attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(...)`.
  `MAX_POLL_ATTEMPTS` is 30 and the tests pass `attempt=1`, so the 204 comes from
  `schedule_panel_fetch` returning falsy after a cache lookup that missed data the test just wrote.
  A sibling test seeding `nominatim` the same way passes, so it is source-specific rather than a
  broken mechanism. If the lookup really does miss in production, pin-detail panels silently render
  nothing; if it is only the tests that are stale, they are asserting a contract the code no longer
  has. Either way it needs resolving - flagged here rather than guessed at.

  Confirmed not mine: reverting the SiteSettings request memo in the container reproduced exactly
  the same 5 failures in the two files I re-ran (5 failed / 15 passed both ways).

- **5 infrastructure-stats failures** (`test_site_admin_stats` x4, `test_infrastructure_stats` x1) -
  environmental, and already recorded as such in module 1. They collect live postgres/valkey/celery/
  nginx status, which no test environment can satisfy without those services reachable.

## Unit 08 (deferred item, now fixed): media send-to-wiki no longer downloads in-request

`media_send_to_wiki` looped up to 20 gallery items calling `materialize_media_item`, each a remote
download, inside the request handler. Two problems, both user-visible: a multi-second hang with no
progress indicator, against the project standard that says anything non-instant shows one; and a
request that times out partway attaches some photos and drops the rest silently while the frontend
toast reports success for all of them.

Split the way `cache_media_item_into_album` already splits the same work - validate and enqueue in
the request, download in `tasks.cache_media_item_into_wiki`, which tolerates the wiki or profile
being deleted between enqueue and run. The endpoint now returns `{"queued": N}` and the toast says
the photos will appear shortly rather than claiming they are already there. Eight tests cover it,
including that nothing is materialized in-request, that the 20-item cap still holds, and that a
malformed entry is reported without stopping the rest of the batch.

## The 12 panel 204 failures: stale tests, not a product bug (2026-08-06)

Verdict: **the panels behave correctly; the tests predated the REData migration.** Nearly every
info panel is REData-backed now, and each one's `gate()` ends in `redata_configured()`, which reads
`UL_REDATA_API_URL`/`UL_REDATA_API_KEY` live from app settings. Neither is set in a test
environment, so the gate refuses and `panel_info` degrades to a quiet 204 - exactly right for an
install with no REData, and invisible to a test written when the panel simply rendered. The sibling
test that passes seeds `nominatim`, which is not REData-backed; that is the whole of the
source-specific split.

Fixed by adding a `RedataConfiguredMixin` (`tests/hypothesis/redata_helpers.py`) to the four
affected classes. It patches the two *settings values* rather than the `redata_configured` symbol,
because every plugin module imports that function by name - patching it at its origin would miss
them all, and patching per module means enumerating every module a test happens to touch. The
function reads settings at call time, so setting them covers all of them at once. Tests that
specifically want the unconfigured behaviour keep patching their own module's symbol, which is the
existing convention (see `test_inaturalist_panel.py`).

Two of the twelve survived that fix, for a second and unrelated reason:
`PinPanelLiveRefreshTests.setUp` never called `super().setUp()`, so the mixin - and the project's
own `TestCase.setUp` - never ran for that class. Added the call.

All 50 tests across the three files now pass.

Worth recording how this was found, because the first three attempts were wrong. I hypothesised in
turn that the Celery broker was unreachable (plausible - `safely_enqueue_task` catches the guard's
`RuntimeError` and reports "broker unreachable", and the view treats that as "give up quietly"),
that `render_context` had changed shape, and that the baker recipe placed the pin at null island.
Each was consistent with the symptom and each was wrong. What settled it in one run was a throwaway
probe test printing the actual intermediate values - `gate(pin): False` - rather than more reading.
Reaching for that earlier would have saved three test cycles at ~3 minutes each.

The broker change made while chasing the first hypothesis was kept: pointing `CELERY_BROKER_URL` at
`memory://` in tests is correct on its own merits, for the same reason as the cache - `apply_async`
should not need a live service, and a test asserting that a request only *scheduled* work now sees
that rather than a swallowed connection error.

## Unit 20 (deferred item, now fixed): pin creation no longer blocks on an AI call

`PinSerializer.create()` called `AutoTagService().suggest_for_pin(pin, apply=True)` inline. That
service runs a keyword stage and then, for any label the keywords missed, calls the LLM gateway -
a network round-trip. So every pin created through the REST API, or through an import that goes
via this serializer, waited on an LLM before the response returned.

This is the recurring two-call-sites shape again, and the fix was already sitting there: the task
`tasks.suggest_pin_category` exists and both other creation paths
(`services.pins.pin_creation` and the Google Maps import) already enqueue it. Only the serializer
was left behind. It now enqueues the same task.

Nothing in the response depended on the tagging having run - the previous code swallowed the
service's exceptions, so a failed tagging attempt was already invisible to the caller. Four tests
pin the new behaviour, including that the pin is still created when the broker is unreachable
(`safely_enqueue_task` returns None) and that a create still isn't treated as an explicit rename.

## Unit 09/10 (deferred item, partly fixed): partial bulk actions now say so

`PinSuggestionBulkActionView` skips ids it cannot act on and logs any that raise, then returns
`processed` alongside `requested`. The page reported only `processed`, as an unqualified success -
so accepting 3 of 5 rendered "Accepted 3 suggestions", identical in shape to accepting all 5. The
user is handed a number with nothing to compare it against, and the two that failed are invisible.
Same silent-partial-failure shape as Unit 08's timeout.

The backend contract was already sufficient; only the toast ignored half of it. It now warns rather
than congratulates when `processed < requested`. Four tests pin the contract the toast depends on -
a whole batch, another profile's id being counted but not acted on, one raising item not stopping
the rest, and accept actually marking the suggestion handled.

Still open from this item: `TripActivity.order` has no uniqueness constraint or locking, so two
concurrent reorders can interleave. That is the same check-then-act family the second pass covered,
and is left for a dedicated pass rather than bundled in here.

## Unit 09/10 (second half, now fixed): trip activity ordering was unserialised

Two check-then-act sequences share the `order` column, and a threaded test reproduces both.

`reorder_activities` validated that the submitted ids are an exact permutation of the trip's
non-completed activities, then applied the positions with one `update()` per row, holding nothing
in between. Two members dragging the itinerary at once interleave: the observed result was
`[1, 2, 3, 3]` - one position written twice, another lost entirely, and a final order neither
member asked for. An activity added between the check and the writes also invalidates the
permutation the check just approved.

`create_activity` appended at `order=trip.activities.count()`, so two concurrent adds both read the
same count and both took that position. The same `count()` backs the `max_trip_activities` quota,
so the pair could also both pass a quota only one of them should have.

**A unique constraint on `(trip, order)` is not the fix**, which is worth recording because it is
the obvious first idea and it would break the feature. Positions are applied one row at a time, so
a partial permutation legitimately collides mid-loop; and reordering only covers non-completed
activities, leaving completed ones holding positions that overlap the reassigned range by design.
Both would violate the constraint during normal use.

Serialising on the parent trip - the same shape as the Consensus tentative-answer fix - matches how
the data is actually used. In `create_activity` the lock is entered *after* place resolution, so it
covers only the read-then-write section rather than a geocoding round-trip. The trip controller's
own 92 tests pass alongside the three new race tests.

## Unit 24/25 (deferred item, now fixed): round generation re-ran the eligibility query per retry

`generate_round_content` retries up to `_MAX_LOCATION_ATTEMPTS` (25) times, skipping any location
the mode cannot build a round from. Every attempt re-ran `eligibility.eligible_locations` - a
multi-join across every participant's pins, and optionally their visits, a label filter and a geo
bound - so generating a single round could cost 25 executions of the most expensive query on the
game path. It runs for every round of every session, and a prior fix already had to attack a
related O(pool size) problem inside `pick_next_location` for the same reason (see its comment about
`/start/` being slow-to-timing-out).

Nothing eligibility depends on changes between attempts; only the caller's own exclusion list
grows. The eligible ids are now resolved once and each attempt narrows a plain primary-key
queryset. Deliberately not converted to a list: `pick_next_location` applies a PostGIS proximity
filter (`point__distance_gte`) to the candidates, and reimplementing that distance check in Python
would put a second, divergable copy of the rule in the codebase - a cheap `pk__in` queryset keeps
the existing contract intact.

Verified both ways: the new test reports `4 != 1` against the previous code and passes with the
change; all 321 SpotGuessr tests pass.

Trivia's equivalent path was checked and left alone - `eligible_questions` is called once per round
with a growing exclusion set, not inside a retry loop, so it does not have this shape.

## Final full-suite verification (2026-08-06)

**6 failed, 10,060 passed, 1,428 subtests passed** in 57 minutes, against a recorded baseline of
18 failed / 10,025 passed. Twelve of the baseline's eighteen are fixed, none were introduced, and
the extra 35 passing tests are the regression tests this audit added.

The six remaining were all present in the baseline:

- **5 infrastructure-stats tests** (`test_site_admin_stats` x4, `test_infrastructure_stats` x1) -
  environmental. They collect live postgres/valkey/celery/nginx status and cannot pass without
  those services reachable from the test process. Not a code defect.
- **1 settings subtest** (`show_supporter_badge`) - a genuine, small API bug, now fixed. The field
  was added to `Profile`, to the `SETTINGS_FIELDS` allowlist and to migration 0031, but not to the
  external API's settings serializers, so `read_settings` produced it and the serializer dropped it
  again: a preference an external client could neither read nor write. Added to both the read and
  write serializers, in the position the allowlist declares it. This is exactly the failure mode
  the serializer's own docstring predicts ("a new preference is invisible here until deliberately
  added to SETTINGS_FIELDS *and* to this class") - the fail-closed design worked, the second step
  was just missed.

With that fixed, every remaining failure in the suite is environmental. The audit's cumulative
work - 11 modules, a second pass, and six deferred Unit items - holds against a full run.

## The last 5 failures were a real bug, not an environment limitation (2026-08-07)

I had recorded the five infrastructure-stats failures as environmental - "they need live
postgres/valkey/celery/nginx". Looking properly, the reason they failed is a genuine product
defect: **the site-admin status page 500s when an infrastructure service is unreachable.**

`collect_infrastructure_service_stats` called its four collectors with no isolation. Each collector
handles the failure it expects (Valkey catches `RedisError`, and so on), but anything else - a
malformed URL, a DNS error surfacing as `OSError`, a driver raising something new after an upgrade -
propagated straight out and took the whole page with it. That page exists precisely to tell an
admin which component is unhealthy, so a component being unhealthy in an unanticipated way hid the
state of the three that were fine along with the one that wasn't. Exactly the per-item isolation
gap found in the safety sweeps (module 9).

Each collector now degrades to an "Unavailable" stat of its own. The page always returns four
entries in display order, however badly the services are behaving - and the five tests pass,
because the network guard's `RuntimeError` is now reported as a degraded service rather than
crashing the request. Fixing the product fixed the tests; mocking the tests would have left the
500 in place.

Two new tests cover the resilience directly: one collector raising must not lose the other three,
and all four raising must still return four entries.

Worth noting a mistake in my own first attempt: I put the four collectors in a module-level tuple,
which captured the function objects at import time and silently made them unpatchable - my own new
tests failed because `mock.patch` on the module attribute had no effect. Naming them inside the
function keeps the reference resolving at call time. An abstraction that breaks testability is
worse than the repetition it replaced.

**The suite now has no known non-environmental failures.**

## Same fan-out gap on the Memories feed (2026-08-07)

The site-admin finding generalised on the first place I looked. `get_memory_events` merges four
independent sources - routes, trips, visits, photos - with no isolation, so any one raising (a
corrupt row, a missing relation, a geometry error) discarded the other three and 500'd the feed on
both the HTML page and the external API.

This is the module's own advertised extensibility seam: its docstring says adding a memory type is
one new function in the source list and nothing else changes. Unguarded fan-out makes that seam a
liability - a bug in a newly added source takes out the three that already worked. A Memories page
missing one kind of memory is worth far more than no page at all.

Each source is now drained independently. `extend()` consumes a generator incrementally, so a
source that fails partway keeps whatever it already yielded rather than losing that too - pinned by
a test rather than assumed.

**`_EVENT_SOURCES` had the same import-time capture problem** as the infrastructure collectors, and
this time the tests caught it before the fix landed: two of the four new tests passed/failed in a
pattern that only made sense if `mock.patch` was having no effect, which is exactly what a
module-level tuple of function objects causes. Replaced with `_event_sources()`, resolved per call.
Twice in one session, so it is worth stating as a rule: **a module-level tuple of function
references is a testability trap** - it freezes the bindings at import and silently ignores
patching. Name the functions inside the function that uses them.

Verified clean: the Private Pin page's street-view provider fan-out already isolates per provider
and records an `ok=False` result for the admin debug overlay, which is the pattern the rest should
match.

## Two more unguarded fan-outs, found by an AST sweep (2026-08-07)

An AST sweep for `for <source|provider|panel|handler> in ...` loops that invoke the loop variable
with no `try` inside found six candidates; two were real.

- **`panel_readiness` (Private Pin page)** - the most consequential of the three. It builds the tab
  strip for the app's busiest page, and it calls `is_ready(pin)` per *bespoke* panel with no guard.
  Panels are the plugin extensibility surface, so a single plugin raising there returned a 500 for
  the whole pin page instead of affecting its own tab. Failures are now logged and treated as "not
  ready", which is the safe default: the tab shows its pending state and polls, exactly as it does
  for a panel whose data genuinely hasn't arrived yet. The cache-backed and slide-backed panels
  were already fine - those resolve through one bulk query rather than a call per source.

- **`get_journal_entries`** - same shape, and its own docstring already argued the case: it
  explains that an *unknown* source key degrades to fewer entries "rather than a 500". A
  *registered* source that raised did 500. Now isolated per source.

Verified clean: the street-view provider fan-out already isolates per provider and records an
`ok=False` outcome for the admin debug overlay. The label-merge and pin-bulk loops the sweep also
flagged iterate the user's own selected rows inside a transaction, where aborting the whole
operation is the correct behaviour, not a bug.

A note on the panel test, which failed on its first run for an instructive reason: I wrote the test
panels as `InfoPanelSource` subclasses, but that inherits `LocationCachePanelSource`, so they were
resolved by the bulk cache query and their `is_ready` was never called - the test exercised a
branch the fix doesn't touch and reported the fix as broken. Checking the class hierarchy rather
than re-reading the fix settled it immediately. Test doubles have to sit in the same branch of the
hierarchy as the code under test, or they quietly prove nothing.

## Query amplification on the map payload (2026-08-07)

Measured rather than read: build N items, count queries, add more, count again, require no growth.
Three of the four measured paths are flat. The map's pin payload was not - **exactly one extra
query per pin**, on the highest-traffic serialization in the app.

The probe named it in one run: `SELECT ... FROM dashboard_wikis`, 3 -> 6 as pins went 3 -> 6.
`serialize()` reads `pin.effective_name`, which falls through to `Location.display_name`, which
reads the reverse OneToOne `wiki`. `prepare_queryset` select_related `location` but not
`location__wiki`. `Location.display_name`'s own docstring asks callers to
`select_related("wiki")` in bulk "to avoid an extra query per row" - the guidance was already
written down, and the busiest caller in the app was the one that missed it. Same family as the
notification-dropdown N+1 from module 7: a reverse OneToOne that reads like an attribute.

Added `test_query_amplification.py` as a standing guard over the map payload and the pin detail
page (flat in both label count and visit count).

Two notes on getting the measurement right, both of which cost a cycle:

- The first harness demanded a delta of exactly zero and reported *-2* on the Private Pin page. That
  is warm-up, not a fix: the first request of a test populates per-process caches. The harness now
  measures a discarded warm-up call first and asserts one-sided - cost must not *grow*; a page
  getting cheaper is never the bug being hunted.
- The first version of the map test called `service.all(service.prepare_queryset(qs))`, but `all()`
  applies `prepare_queryset` itself. The doubled `Prefetch("labels", ...)` raises
  `ValueError: 'labels' lookup was already seen with a different queryset` at evaluation. That was
  my error, not the code's - but it is worth knowing the API raises rather than silently
  double-prefetching.

## Query amplification on the external API's trips list (2026-08-07)

Extending the harness to the wiki page, trips detail page and the API list endpoints found the
pages clean - flat in comment, alias, link, edit-history, activity and member count - and one more
real defect. **The API's trips list cost six queries per trip** (24 -> 42 for three more trips), and
the probe split it immediately: five against `dashboard_trip_activities`, one against
`dashboard_trip_memberships`.

- **Five per trip: the effective-date properties.** `TripSummarySerializer` exposes
  `effective_start_date`, `effective_end_date`, `timeline_status` and `duration_days`, and the last
  two each read *both* of the first two - which fall back to querying the trip's activities. This
  is the same defect fixed in the Memories feed in module 9, resurfacing on a different surface
  because that fix was local to the aggregator.

  Fixed at the model this time instead of at each call site: both properties now prefer a
  `_eff_start`/`_eff_end` annotation when the queryset supplied one, and otherwise compute once and
  memoize on the instance. So a list of trips is flat wherever it is annotated, *and* a single trip
  costs one query however many of the four fields are read. `for_list_page` now supplies the
  annotations, matching what the Memories aggregator already annotated - including using the later
  of `scheduled_at` and `scheduled_end`, so the two surfaces agree on when a trip ends.

- **One per trip: the viewer's membership.** `_membership()` ran a targeted query per trip even
  though `for_list_page` prefetches the whole roster - the row was in memory already. It now reads
  the prefetch when one exists (checked via `_prefetched_objects_cache`) and keeps the targeted
  query otherwise, so callers that did not prefetch don't start pulling whole rosters.

The general lesson, now seen three times (Memories trips, the map's `location__wiki`, this): **an
expensive model property is invisible at the call site.** `serializer.field = source="x"` looks
free. Making the property itself annotation-aware and memoized fixes every present and future
caller, which a per-call-site prefetch does not.

877 trip/memories tests pass alongside the new guards.

## Frontend layer: the inline-JS mass (2026-08-07)

Measured, since the project's stated preference is HTMX first and TypeScript only where HTMX
cannot do the job: **31 templates carry more than 120 lines of inline `<script>` each, totalling
19,638 lines.** The worst is `pages/map/index.html` at 5,052 lines of inline JS in a 5,691-line
template - 89% of the file - followed by `themes/base.html` (1,882), `pages/messages/index.html`
(1,772), `pages/trips/detail.html` (1,380) and `pages/location/index.html` (1,297).

For comparison, the entire typechecked TypeScript tree is 59 files. `tsconfig.json` includes only
`frontend/ts/**/*.ts`, so **none of those 19,638 lines are typechecked, bundled, minified, or
reachable by `bun test`.** Every defect this audit found in that layer - the pin-cache version
drift, the duplicated trip-name list, the pin-delete cache invalidation - lived in inline template
JS, which is consistent with it being the least-guarded code in the repo.

This is the largest maintainability gap found in the audit, but "move it to TypeScript" is a
programme of work rather than a fix, and which parts become HTMX versus bundled modules is a
design call for the maintainer. Recorded with numbers so it can be prioritised, along with the
obvious first candidate: `base.html`'s 1,882 lines are shared by every page.

**Correction to the paragraph above**, which originally also said the dev toolbar's 591 lines
"ship to production for a development-only feature". They do not. `base.html` includes that partial
only under `{% if show_dev_toolbar %}`, and `SiteSettings.show_dev_admin_features` grants it to
site admins solely when the effective environment is development or local, and to non-admins only
when `UL_ALLOW_DEV_TOOLBAR_FOR_NON_ADMINS` is set *and* the environment is
staging/development/local/testing. Production is excluded on both paths. The toolbar is still worth
extracting so it is typechecked, but the reason given for prioritising it was wrong, and correcting
it changes which target is actually valuable: `base.html`, which does render on every page.

Checked and deliberately *not* changed: six templates hardcode the values of a server-side
`TextChoices` in their JS (`MapViewChoice` and `MapCenterMode` fully, `ThemeChoice`,
`MapLayerMode`, `DirectMessageShareKind` partially). It reads like the pin-cache version
duplication, but it is not the same class of defect: those values are stable identifiers used for
genuine branching (keyboard shortcuts, layer switching), and adding a new choice leaves the
existing branches working rather than silently disabling a feature. Coupling worth knowing about,
not a latent bug, and not worth the indirection of emitting them through `json_script`.

### Extracting base.html's inline JS: the pattern and the constraint

The codebase already has the right pattern, and documents it: `entries-classic/core.ts` is a
classic (non-module) IIFE bundle loaded in `base.html`'s `<head>`, installing window globals via
`installGlobalX()` helpers from `shared/` modules. Its own docstring explains why it is not
`type="module"` - module scripts defer until after parsing, and several pages call these globals
from classic `<script>` tags that run synchronously. So extracting base.html's remaining globals is
"add a `shared/x.ts` with an `installGlobalX()`, import it in `core.ts`, delete the inline block".

`base.html` exposes 15 such globals. The dialog group is the obvious first extraction -
`confirmDialog` (used by 10 templates), `deletePinCascade` (4), `urbanlensConfirmExternalLink` (2) -
because it is self-contained, has no Leaflet dependency, and already has a contract test.

**But the naive move is wrong, and this is the part worth writing down.** That block is not just
function definitions: its IIFE resolves `#confirm-dialog` and its buttons *at load time* and
attaches listeners to them, and it sits after the `<dialog>` markup in the body. `core.ts` runs in
the `<head>`, where those elements do not exist yet - so lifting the block as-is yields null
element references and a dialog that never opens. The extraction has to bind lazily (resolve the
elements and attach listeners on first use) before it can move.

Not attempted here for a reason worth stating: this repo has no DOM/browser test layer, so a
behavioural change to a dialog reached from 10 templates could not be verified beyond typechecking,
and `bun test` would report success either way. The contract tests added by this audit only parse
templates for invariants. Specifying the constraint is more useful than a plausible-looking change
nobody can check - and it makes the case that a small DOM-level test setup would pay for itself
before this 19,638-line layer is moved.

## A DOM test harness, and the first extraction it made safe (2026-08-07)

The blocker recorded above was real, so it got fixed first: `bun test` had no document, so
anything touching the DOM could be typechecked but never *exercised*. Added
`@happy-dom/global-registrator` as a dev dependency and a three-line preload
(`frontend/ts/testing/dom-setup.ts`, wired through a new `bunfig.toml`).

It immediately paid for itself twice over:

- `pin-cache.test.ts` carried a hand-rolled `MemoryStorage` polyfill assigned onto `globalThis`,
  which now conflicted with the real one - so those seven tests were rewritten against an actual
  `Storage` rather than a stand-in.
- The `base.html` dialog group moved out: `shared/confirm-dialog.ts` now owns `confirmDialog`,
  `urbanlensConfirmExternalLink` and `deletePinCascade`, installed by `entries-classic/core.ts`,
  binding `#confirm-dialog` **lazily on first use** exactly as the constraint required. 107 lines
  of inline JS left `base.html` (1,882 -> 1,776), and 12 behavioural tests now cover what was
  previously untestable: cancel resolves false, the alt button resolves `"alt"`, the message is
  HTML-escaped, a 409 asks about children and retries with the chosen mode, a failed delete does
  not flag the cache, and - the one that matters most - binding still works when the markup appears
  after the module loads.

  The old `pin-delete-invalidation.contract.test.ts`, which parsed `base.html` looking for the
  string `ul_pins_dirty`, was deleted: the DOM test asserts the actual behaviour it was
  approximating.

**The typechecker found a latent type lie on the way through.** `types/globals.d.ts` declared
`window.confirmDialog` as returning `Promise<boolean>`, but the implementation could already return
`"alt"` when a caller passed `altLabel` - which the pin-delete "keep child pins" path does. Any
caller narrowing on that type was trusting something untrue, and `"alt"` is a truthy string, so it
would have read as a confirmation. Corrected the declaration and made `dialogs.ts`'s
`confirmAction` narrow explicitly rather than by assumption.

That is the argument for the harness in one paragraph: moving 107 lines under the typechecker
surfaced a wrong type declaration that had been sitting in a `.d.ts` unexercised. There are 1,776
lines left in `base.html` alone.

### base.html's remaining 1,776 lines, block by block

Surveyed rather than attacked, so the next extraction starts from evidence. The file has eight
inline blocks left:

| Block | Lines | Globals | Extractable? |
| --- | --- | --- | --- |
| 1 | 158 | `urbanlensMediaThumbFallback`, `urbanlensSizeEditInPlaceInput` | Yes, but carries `{{ csrf_token }}` and Django `messages`, so it needs a `json_script` config first |
| 2 | 251 | none | Probably - no globals to keep in step |
| 4 | 62 | `autosaveGuard` | Yes, small and self-contained |
| 6 | 936 | the comment-map composer group | **Not as one move** - see below |
| 7 | 307 | `ulSectionCollapsed`, `ulRefreshCollapseRestore`, `ulFlyToToolsFab` | Best next target - no template tags, no Leaflet |
| 8 | 24 | none | Trivial, but 9 template tags for 24 lines - leave it |

**Block 6 (936 lines, the comment map composer) should not be moved as a single step.** It is one
IIFE whose functions share closure state, it drives Leaflet through `typeof L` guards, and it
embeds seven `{% url %}` tags plus the viewer's profile uuid. Moving part of it would leave the
group's globals defined in two places, which is worse than leaving it; moving all of it would put
~900 lines of Leaflet-dependent behaviour under a test harness that has no Leaflet. It needs its
own plan, and probably a Leaflet stub, before it is touched.

**Block 7 is the right next one** - 307 lines, no template tags, no Leaflet, mostly `localStorage`
and class toggling that happy-dom tests well. One caveat found while reading it: about 70 of those
lines (`ulFlyToToolsFab` and `_toolsFabTarget`) measure layout via `getBoundingClientRect` and
`getComputedStyle`, which happy-dom returns as zeros - so that part would move unverified. The two
concerns share no closure state, only DOM lookups, so they can go as two modules with the
animation helper's move made deliberately and knowingly untested rather than by accident.

Not started this turn rather than half-finished: a 307-line port left incomplete is worse than one
not begun, and the survey is what the next attempt actually needs.

## Full-suite verification (2026-08-07): 10,087 passed, 0 failed

**10,087 passed, 0 failed, 1,429 subtests passed, in 58:49.** Not one failure line in the output.
The progression across the audit:

| Run | Failed | Passed |
| --- | --- | --- |
| First complete run (2026-08-06) | 18 | 10,025 |
| After the panel-test and settings-serializer fixes | 6 | 10,060 |
| This run | **0** | **10,087** |

Of the original eighteen, twelve were stale panel tests written before the REData gate existed, five
were the infrastructure-stats tests that turned out to be masking a real 500, and one was a settings
field the external API had never exposed. None were flaky; each had a cause.

**One caveat, stated because it affects how much this run proves.** Files were copied into the
container while it was running - the panel-contract validation landed mid-run - so this is not a
pristine single-commit run. pytest imports test modules at collection, but source modules are
imported lazily, so a mid-run copy can produce a mixed state. The container's Python tree is
byte-identical to the repo now, and the affected work (`panel_source_problems` and its ten tests)
was verified separately, but a run that proves a specific commit end to end needs no concurrent
copies. Worth doing before a release; not worth re-running an hour for now.

Also unaffected by that caveat: the frontend work landed today is host-side only (bun), never
copied into the container, so `shared/confirm-dialog.ts` and the happy-dom harness are outside this
run entirely. They are covered by `bun test` (162 passing) and `tsc --noEmit` (clean).

## Two more bugs found extracting base.html's guard/scroll/dialog blocks (2026-08-07)

Both were live in `base.html`'s inline script, both are now fixed with tests.

**1. Scroll-to-hash threw a `DOMException` on any fragment that is not valid CSS. FIXED.**
`_scrollToHash` called `document.querySelector(window.location.hash)` directly. A fragment
is not a selector: ids beginning with a digit, `#/route`, `#foo=bar`, `#!`, `#sec:2` and -
most relevantly - the `#_=_` and `#access_token=...` that OAuth providers append when
redirecting back all throw. This app signs in through Google and Discord, so that path is
reachable in production. Because the handler is bound to `htmx:afterSettle`, it threw again
on *every* HTMX swap for the life of the page.

Verified by probe before fixing: of eight realistic fragments, five threw. Fixed by trying
`getElementById` on the decoded fragment first (it takes a literal id, so it copes with all
of the above), keeping `querySelector` behind a `try` for fragments naming something other
than an id, and guarding `decodeURIComponent` against malformed escapes. A numeric id such
as `#123` now actually scrolls, where before it threw.

*Testing note worth remembering:* the first version of this test wrapped `dispatchEvent` in
`expect(...).not.toThrow()` and passed while the exception was plainly being thrown - a
listener exception is reported, not propagated, so that assertion can never fail. The test
had to call the function directly. Assertions around `dispatchEvent` prove nothing about
what the listener did.

**2. The unsaved-changes guard hijacked ctrl/cmd/shift-click. FIXED.**
The capture-phase click handler checked the href's scheme and `target="_blank"` but never the
modifier keys or mouse button. With unsaved changes present, ctrl-clicking a link to open it
in a background tab was intercepted, `preventDefault`ed, and - on confirming - navigated the
**current** tab via `location.href`. So the one gesture that would have safely left the page
alone instead destroyed the changes the guard exists to protect. Fixed by returning early on
`ctrlKey`/`metaKey`/`shiftKey`/`altKey`/non-primary button.

## The leave-page warning existed three times, with the same two bugs in each (2026-08-07)

Following up the ctrl-click bug found in `base.html`'s auto-save guard: the same
"confirm before leaving" logic had been copy-pasted into two more pages, and both copies
carried the identical defects.

  base.html            auto-save guard (unsaved / in-flight changes)
  safety/detail.html   "nobody would be notified if something happened"
  tools/index.html     an export is still running

**Bug 1 - modifier clicks were hijacked.** All three checked the href's scheme and
`target="_blank"` but none checked modifier keys or mouse button. Ctrl/cmd-clicking a link
to open it in a background tab was intercepted and, on confirming, navigated the *current*
tab via `location.href` - so the one gesture that would have left the page safely alone
instead destroyed what the guard was protecting. Worst on the safety page, whose entire
premise is that leaving means nobody can reach you.

**Bug 2 - the user was asked twice.** The safety and tools copies navigated via
`location.href` without first clearing their blocked condition. That navigation re-enters
`beforeunload`, which was still blocked, so the browser's own "Leave site?" prompt appeared
on top of the one just answered. The safety page even has a `_safetySkipLeaveWarning` flag
for exactly this, set on form submit and status update - but never in the link path. The
auto-save guard avoided it only because it happened to call `allowNavigation()` first.

Fixed by extracting the behaviour to `shared/leave-confirmation.ts`, which each page now
configures with just its condition and wording. The suppression is internal, so no caller
can forget it. Also filters `download` links, which save a file without navigating - they
must not be confirmed, because confirming permanently disarms the guard while the page
stays put.

Net: 39 lines of duplicated template JS removed, 25 tests added covering the behaviour that
previously had none.

## Live location sharing on a safety check-in failed silently (2026-08-07)

Found by sweeping for `fetch` call sites that mutate state without checking the response.
Of 34 such sites, most turned out to be false positives (helpers whose callers check, or
deliberately best-effort calls) - but this one is real, and it is on a safety feature.

`fetch` only rejects on network failure, never on an HTTP error status. Both live-location
handlers had a `.catch` and no `.ok` check, so every server rejection was invisible:

- **Position updates** were sent with `.catch(function () {})`. The update endpoint returns
  **400** when sharing has been turned off or the check-in has already concluded. So if a
  check-in was resolved from elsewhere - a partner marking the owner safe, say - the browser
  kept watching the GPS and posting a position every 30 seconds, each rejected, forever. The
  owner saw no indication. The partner watched a marker frozen at the last accepted fix.
- **The toggle** called `startWatching()` unconditionally and only caught network errors, so
  a refused toggle left the switch on and the GPS running while the server had recorded
  nothing.

The drift was visible inside the same file: the delete-check-in handler thirty lines above
correctly does `if (!r.ok) throw new Error()`.

Fixed by moving the logic to `shared/safety-live-location.ts` (22 tests, including one per
rejection status) which checks every response, reverts the toggle when the server refuses,
and warns the owner after two consecutive failed reports - two rather than one because a
single blip is noise, and once rather than every 30s because a warning that repeats is a
warning people learn to ignore. Recovery resets it, so a later outage can warn again.

**Not fixed, deliberately:** the remaining ~30 unchecked mutating `fetch` sites. Each needs
judging on its own - several are intentional fire-and-forget (the profile "skip setup"
button navigates whether or not the save lands) and a blanket change would be churn. Worth
a pass when someone can weigh them individually.

## Six writes reported success for requests the server had refused (2026-08-07)

Triage of the 34 unchecked mutating `fetch` calls found in the previous pass. The question
asked of each: *if this request fails, does the user believe something saved that did not?*

**Six said yes**, all sharing one shape - `fetch(...).then(r => r.json()).then(showSuccess)`
with no `response.ok` check. `fetch` resolves for 400s and 500s, so the success path ran
regardless:

| site | what the user saw on failure |
| --- | --- |
| `map/index.html` star rating | "Rating saved." - and the local pin cache updated with a rating the server rejected |
| `map/index.html` pin field edit | "Name updated successfully" |
| `location/index.html` media relevance | thumb stays marked; reverts on next load |
| `location/index.html` send-to-wiki | "N photo(s) queued for the wiki" - the inner `.json().catch(=> ({}))` swallowed a 500's HTML page and fell through to the success toast |
| `location/wiki.html` photo vote | vote stays applied; reverts on next load |
| `trips/detail.html` marker drag | marker stays where dropped |

The map page is the clearest case of drift: it already had a private `_fetchJson` doing
`if (!resp.ok) throw`, used for its GET reads - while the two PATCH writes in the same file
did not use it.

That helper is now `shared/fetch-json.ts` (25 tests), which throws on non-2xx carrying the
server's own message where it sent one, handles 204 (calling `.json()` on an empty body
throws, which would turn a successful DRF delete into an apparent failure), and applies the
CSRF header. The map's private copy is a two-line shim over it.

**Judged fine and left alone**, with the reasoning recorded so the next pass need not
re-derive it: geolocation visit tracking, map position and dark-mode preference saves (all
explicitly best-effort - nothing is claimed to the user), the profile skip-setup button
(navigates either way by design), and `e2ee-client.ts` (its `postJson` is a helper whose
callers check; the login path checks `.redirected`). The media-sort preference now warns on
failure since it had no handler at all.

## Bulk writes silently skipped the receivers that maintain derived state (2026-08-07)

First backend unit after five frontend ones. Three checks on the Celery/transaction layer came
back **clean**, which is worth recording so nobody re-runs them: of 71 `safely_enqueue_task`
call sites, **none** dispatch inside a `transaction.atomic()` block without `on_commit`
(verified with an AST walk, after a naive line-based scan proved unreliable for the
`@transaction.atomic` *decorator* case); there are no `select_for_update()` calls outside a
transaction; and no Celery task is handed a model instance instead of a pk.

The real finding was elsewhere. `bulk_update`/`bulk_create` issue raw SQL and never fire
`post_save`, so any receiver maintaining derived state is skipped. Four sites did this:

**1-3. `Label` bulk writes left the map pin cache stale.** `Pin.icon_source_label()` picks the
winning label by `-order`, so a label's *order* decides which icon and colour a pin draws -
not just its icon field. `refresh_map_pin_cache_for_label` exists precisely to invalidate the
server-side pin cache when that changes, and its own docstring says pins would otherwise "keep
serving the old baked-in icon/color until the cache TTL lapsed". All three bulk paths went
straight past it:

  - `controllers/organize.py` - drag-to-reorder labels
  - `external_api/views_labels_bulk.py` - the API's reorder endpoint
  - `external_api/views_labels_bulk.py` - the API's bulk edit, which writes `icon`, `color`,
    `order` and `description` directly. The most direct case of all.

So a user reordering their labels saw the map keep drawing the old icons.

**4. Copying a pin list into a trip never reached the calendar.** `copy_list_pins_to_trip`
uses `bulk_create`, so `sync_trip_on_activity_save` never fired and the new activities never
reached an auto-synced Google Calendar until some unrelated activity was saved.

Fixed by giving the label receiver a reusable `refresh_map_pin_cache_for_label_ids()` (one
query for many labels, `distinct()` so a pin carrying two changed labels is refreshed once)
and calling it from all three bulk paths, and by making `queue_calendar_push` public and
calling it once after the bulk_create. 8 tests, including one establishing the premise that
reordering really does change which icon a pin draws - without that, refreshing the cache
would be pointless.

**Worth generalising:** any new `bulk_create`/`bulk_update` on a model with `post_save`
receivers needs this same audit. `Pin` alone has six.

## A guard for the bulk-write class, which immediately found a fifth site (2026-08-07)

Follow-up to the four bulk writes fixed in 8ed25a93. Whether a bulk write is dangerous is a
property of the *model*, not the call site, so a site that is safe today becomes a bug the
moment somebody adds a receiver to the model it writes - and nothing about that change would
look wrong in review. `tests/hypothesis/test_bulk_write_signal_guard.py` now fails the build
on any `bulk_create`/`bulk_update` targeting a model with live `pre/post_save` or
`pre/post_delete` receivers unless the site is listed in `REVIEWED` with a reason.

Design notes worth keeping:
- Receivers are read from **Django's live signal registry** (`signal.has_listeners(model)`),
  not by grepping for `@receiver`. That is what caught the fifth site: `Image` gets its
  `post_save` connected dynamically from the achievements `_SUBSCRIPTIONS` table, so no grep
  for a decorator would ever have found it.
- The allowlist is keyed by `(file, model, operation)`, **not line number**, which would churn
  on every edit above the call and train people to update it without thinking.
- Two "guard the guard" tests assert the scan still finds call sites and the registry lookup
  still returns receivers. These earned their place immediately: the first version of the test
  had a wrong `PACKAGE_ROOT` and scanned **zero** files, which made the real assertion pass
  vacuously. Without those two tests it would have been committed green and guarded nothing.

**The fifth site (`pin_sharing.py`, `Image.bulk_create`) turned out to be correct as-is** -
firing that receiver would credit the *recipient* of a share with a photo-upload streak day
for photos they did not take. It is recorded in `REVIEWED` with that reasoning. Its one loose
end: the recipient's photo-count metric is not invalidated at copy time, and self-heals only
on their next photo action. Left alone as a product question, not a defect.

### But following that thread found two real bugs in the same function

`create_pin_from_share` builds the recipient's `Image` rows field by field, and any field it
omits silently takes the **model default** instead of the source row's value. Two of those
defaults actively misdescribe the copy:

- **`source` defaults to `UPLOAD`.** Resharing a pin whose gallery held a materialised Yelp,
  Wikimedia or Smithsonian photo filed it as the recipient's own upload. `ImageSource` is what
  drives the Media section's per-source tabs, so the photo also landed in the wrong tab.
- **`media_type` defaults to `PHOTO`.** A shared video was recreated as a photo, which renders
  through `<img>` instead of the player - a broken image.

Both fixed by copying `source`, `media_type`, `media_source_key` and `media_item_key`. The
context FKs it omits (`wiki`, `visit`, `safety_checkin`, `direct_message`, `pin_suggestion`)
are correctly dropped - they belong to the sender's row, not the copy.

## The Pin share copy dropped four properties, and now cannot drop a fifth (2026-08-07)

Same technique as the Image copy: load the model, diff its concrete fields against the
kwargs the copy passes, judge each omission. `create_pin_from_share` names 28 of `Pin`'s 43
fields by hand; 11 were omitted. Seven of those are correct (the recipient's own slug, view
and visit state, dismissal flags, the wiki cache). Four were not:

- **`pin_type_is_user_provided`.** The field's own comment says it guards `pin_type`
  "exactly like `name_is_user_provided` guards `name`" - and `name_is_user_provided` *is*
  copied. It is the only thing that makes `classify_detail_marker` return early, so a
  recipient's copy carried the sender's chosen type with no protection and the automatic
  building/parcel classifier was free to overwrite it. Precisely the outcome the flag exists
  to prevent.
- **`custom_icon`.** `Pin.effective_icon` checks `custom_icon` *before* `icon`, so a pin with
  a custom uploaded icon arrived looking like a different pin - while the function's own
  docstring promises it carries over "every user-visible property (name, icon, ...)".
- **`cover_photo`.** Correctly not copied verbatim (it points at the sender's `Image` row),
  but the recipient lost the hero image entirely even though the photos themselves are
  copied. Now mapped to the recipient's own copy by position, and left null when the sender
  shared only part of their gallery and not the cover.
- **`indoor_outdoor`.** The one place-property that did not travel while every sibling
  (`fences`, `alarms`, `cameras`, `security`, `signs`, `vps`, `plywood`, `locked`, the three
  dates) did. Currently inert - the field is groundwork with no UI - so this is consistency,
  not a user-visible fix. Recorded as such rather than overstated.

**The guard matters more than the four fixes.** All of these came from one structural fact:
the copy names fields by hand, so a field added to `Pin` later is simply absent and takes its
default, and nothing in review connects "add a column to Pin" with "update a function in the
sharing service". `SharedPinCopyCoversEveryFieldTests` now fails on any `Pin` field that is
neither copied nor listed in `NOT_COPIED` with a reason, plus two tests keeping that list
honest (no entries for fields that no longer exist, none claiming to skip a field the copy
actually passes).

Mutation-tested before trusting it: removing `slug` from `NOT_COPIED` makes it fail with
`['slug'] != []`. Worth doing - the first version of the bulk-write guard passed while
scanning zero files, and a guard nobody has seen fail is not yet known to be a guard.

## The field-by-field copy class is exhausted; albums audited (2026-08-07)

**Negative result, recorded so nobody repeats the search.** Task #35 planned to apply the
field-diff technique to trip cloning, wiki forking, album copying and the import paths. A
structural scan for the shape - calls with 6+ kwargs where most values are attribute reads
off one source object - found only **four** across the whole tree, and two are the paths
already fixed (`create_pin_from_share`'s Pin and Image copies). The other two are a
model-bakery recipe and a WebAuthn credential built from a library result, neither a
model-to-model copy. Checked the alternative idioms too: no `pk = None; save()` clone, no
`model_to_dict`, no `deepcopy` of instances; the `Model(**kwargs)` sites are serializer
creates. The features the task guessed at simply do not copy models field by field.

### The album feature is in good shape

Audited as the newest code on the release branch, and it holds up better than most of what
this audit has touched. Recorded because "we looked and it was fine" is worth knowing:

- Authorization is centralised in `_resolve_album_owner` and every view routes through it.
- The add endpoint re-scopes posted ids through `eligible_images_for`, so the picker's filter
  is not the only thing enforcing eligibility - the classic version of this bug.
- Setting a cover re-scopes through the album's own contents.
- `cover_image` is `SET_NULL` (with a comment saying why), *and* removal clears it, *and*
  `cover_from_images` falls back when the cover is not in the viewer's visible set. Three
  independent defences for the same failure.
- Duplicate membership is prevented in code *and* by a `uq_album_item` constraint.
- The broker-unreachable path in `_add_external` falls back to running the download inline -
  the exact failure mode that went unhandled elsewhere in this codebase.

### One real bug: a check-then-act race on adding photos

`add_images_to_album` reads which images are already in the album, then inserts the rest,
with no lock and no transaction. `uq_album_item` correctly prevents the duplicate row, but
the unguarded insert turned the loser of that race into an `IntegrityError` - a 500 rather
than a no-op.

Not hypothetical: there are two callers, and one is the Celery task
`cache_media_item_into_album`. Celery delivers at least once, so a redelivered task races
both itself and the user adding the same photo from the picker once it materialises.

Fixed with `ignore_conflicts=True`, plus counting the rows actually inserted rather than
returning `len(to_add)` - with conflicts ignored, the length over-reports what the call did,
which would have shown "2 photos added" when one was already there.

## Check-then-act sweep: one fix, and a hypothesis that was wrong (2026-08-07)

Following up the album race (42a178fa) by looking for the same shape elsewhere: read what
exists, insert the difference, with a unique constraint behind it.

**A wrong hypothesis, recorded so nobody re-derives it.** The first scan listed 77 models
with a multi-column unique constraint and flagged 17 unprotected creates. It looked like
`add_group_members` was a *deterministic* bug: `GroupChatMembership` is documented as one row
per member *stint*, re-adding is meant to create a brand-new row, and a `(group, profile)`
constraint would make that impossible - so re-adding anyone who had left should 500.

It does not, because that constraint is **partial**: `(group, profile) WHERE left_at IS
NULL`. That is exactly the right schema - one active membership, unlimited historical stints
- and my scan had simply not looked at `condition`. **27 of the 77 constraints are partial**,
so any future sweep must read the condition before concluding anything. The group-chat stint
behaviour is correct, and `test_group_chats.py` already covers both halves of it
(`test_readding_creates_new_stint`, `test_rejoined_member_does_not_see_absence_window`); the
tests I had written were deleted rather than committed as duplicate coverage.

**What actually discriminates a real instance from a theoretical one is a Celery caller.**
The album bug mattered because `cache_media_item_into_album` is a task and Celery delivers at
least once, which makes "two runs at once" ordinary rather than a rare interleaving. Applying
that filter to the 17 candidates leaves exactly one: `generate_keywords_for_image`, whose
only caller is the `generate_photo_keywords` task.

It deletes a provider's existing keywords and inserts the new ones. The delete does not
isolate it from another worker doing the same, so the second insert hit
`uniq_image_keyword_per_source` and failed the task, leaving that provider's keywords
missing until something re-ran it. Fixed with `ignore_conflicts=True`.

The remaining 15 need two genuinely simultaneous user actions (two managers adding the same
person, a double-clicked "add to list"). Left alone deliberately: the failure is a 500 on one
of two racing requests, the constraint keeps the data correct either way, and blanket-editing
15 call sites would be churn of the kind this audit has been avoiding. Worth revisiting only
if one of them acquires a Celery caller.

## External API audit: no changes warranted (2026-08-07)

A full unit spent on the external API's permission, scope and throttle layers found nothing
to fix. Recording what was checked and how, because the useful output of a clean audit is
knowing not to repeat it.

**Scope coverage is complete.** All 205 view classes under `external_api/` declare
`required_scopes` somewhere in their ancestry. Verified by walking the AST and resolving
inheritance transitively.

**Everything fails closed, in three independent places.** `credential_grants` refuses an
empty requirement rather than reading it as "nothing required"; `HasApiKeyScope` defaults a
missing `required_scopes` to an empty set, which that refusal then denies; and
`ExternalApiView.required_scopes` returns an empty set for a method with no entry. A view
added without a declaration is unreachable to credentials, not open to them.

**The PAT/OAuth2 split is enforced at check time, not at grant time.** `OAUTH2_ONLY_SCOPES`
is rejected in `credential_grants` even if a key somehow carries it, so a hand-edited row or
a future scope-picker bug cannot hand a bearer key access to end-to-end encrypted DMs.

**Object-level ownership holds.** A scan for handlers doing a lookup with no owner-scoping
token returned three; all three are false positives that scope through a base-class helper
(`_get_checkin`, `resolve_solo_session`) and then filter by that already-scoped parent.

**Throttle tiers derive from the same declaration that gates access**, so there is no second
list to drift, and an undeclared method lands in the *tighter* write tier. Only two views
override the tier and both are documented. The one shape worth checking - a read-tier GET
that triggers paid external calls - is `PinPanelDetailView`, and it is bounded twice over:
`schedule_panel_fetch` is single-flight per (source, pin) via a cache marker, and the fetch
itself runs under the per-service rate limiter (`RateLimitExceededError` is caught in
`external_data`).

### A lesson about these scans, learned twice

Two scans in this audit produced confident wrong answers because they matched on syntax
without handling a variant:

- The unique-constraint scan ignored `UniqueConstraint.condition`, so 27 *partial* constraints
  read as absolute - which is what made a correct group-chat design look like a bug.
- This scan resolved base classes from `ast.Name`/`ast.Attribute` only, so six views
  inheriting a *subscripted generic* (`PinSubResourceView[PinNote]`) reported no bases at all
  and looked unprotected.

Both times the fix was to widen the scanner, and both times the first result was plausible
enough to act on. Any structural scan here should be re-run against a known-good and a
known-bad example before its output is trusted.

## The data export disclosed trip members the app masks on screen (2026-08-07)

Audited the import/export paths. Most of it is exemplary and needed no changes - recorded
here so it is not re-audited:

- **Archive handling is properly defended.** No `extractall` anywhere; members are iterated
  and extracted individually. `_safe_basename` keeps only the basename (so there is no path
  left to traverse) *and* rejects `..`; symlinks are skipped in both ZIP (via `external_attr`
  mode bits) and TAR (`isfile()` only); there are per-file, total-uncompressed and file-count
  caps; and both formats read one byte past the declared size to catch a compression-ratio
  attack where the header lies.
- **The import refuses to write on someone else's behalf.** `_resolve_import_target` requires
  a pin target to resolve to the importer's *own* pin and a wiki target to pass
  `location_visible_to`, with the threat named in its docstring.
- **Friendships import as requests, not facts.** `_import_connections` re-creates each
  outgoing row through `Friendship.request` rather than honouring the exported status, so a
  crafted archive cannot forge an ACCEPTED friendship and grant itself friend-level access.
  The stated principle is the right one: "an import may only re-create actions the importing
  user could take themselves through the UI".

**The bug was on the export side.** `_export_direct_messages` passes each conversation
partner through `display_identity_for` and says why in its docstring - "an export never
reveals a partner's name/avatar beyond what the user could currently see on screen (e.g.
after being blocked or a privacy change)". `_export_trips` did not apply that rule: it wrote
`p.user.username` for every trip member and for the creator, while the trip page resolves
those same people through `resolve_visible_identities` and masks the ones the viewer may not
identify.

So a user could export their data and read the username of a co-member whose profile
visibility hides them - `NO_ONE`, or any setting the viewer doesn't satisfy. The codebase
already agreed this was wrong; only this one function had not been told.

Fixed by resolving member and creator names through `resolve_visible_identities`, the same
call the trip page makes. `member_uuids` is still exported for masked members: it carries no
name, and the import's re-invite step reads *that* field rather than `members`, so masking
the names cannot break the round trip - verified against `_import_trips`, which reads
`member_uuids` and never `members`.

Seven tests, including one asserting the DM export still masks (it already did - a guard so
the two exports cannot drift apart again) and one asserting masked members are still *listed*,
since dropping them would leak a different fact: how many people are on the trip.

## Global search named people the rest of the app masks (2026-08-07)

Generalising the trips-export leak (ea1a62db). If one surface had missed the
identity-masking rule, others might have, so this was a sweep rather than a guess: find
every function that emits another party's `username` without calling a masking helper. 48
matches, most correct by design - `__str__` for admin, notification text addressed *to* the
person concerned, URL and query parsing that merely mentions the word.

**Three were real, all in global search.** Search results name other people, and every other
surface that does resolves the name first:

  messages page + DM export      display_identity_for
  trip comments                  resolve_visible_identities
  pin/wiki comment list          resolve_visible_identities (the template branches on
                                 the `is_masked` it sets)

`DirectMessageSearchProvider` built `title=f"{direction} {other.username}"`, and
`CommentSearchProvider` built both its subtitles from `.username` - for pin/wiki comments
and for trip comments. So a user could type a word into the search box and read the username
of a conversation partner or comment author whose profile visibility hides them; the page
rendering those very same rows would have shown "Member 2".

Worth noting the reach: global search is also exposed through the external API, where the DM
provider is gated behind `messages:read` (an OAUTH2_ONLY scope) precisely because it returns
message content. The names were leaking through the same endpoint.

Fixed with one `_display_names` helper used by all three call sites, resolving a whole batch
in a single call - the resolver recomputes the viewer's allowed-subject set per invocation,
so per-row resolution would have been both wrong-shaped and slow. Six tests: two that fail
without the fix, two asserting a *visible* author's name still appears (masking everything
would be a different bug), and one asserting rows are still returned - the searcher is
entitled to find the comment, just not to learn who wrote it.

### Two existing tests were asserting the leak

`test_messages_from_person_finds_conversation_without_text_terms` and
`..._excludes_other_conversations` both checked `self.alice.username in result.title`. With
masking applied they fail with `'alice' not found in 'From Member 1'`.

Those tests are about *filtering* - that a person-scoped query returns only that person's
conversation - and were using the title as a convenient proxy for "whose conversation is
this". The proxy was the leak. They now assert on the result's URL, which carries the
partner's profile slug, so the original intent is tested without requiring the name to be
disclosed. Rewriting a test to match new behaviour deserves suspicion, so to be explicit:
the assertion that changed was incidental to what each test is named for, and the fixture
profiles are genuinely masked from one another (they are unrelated `baker.make` profiles),
which is why the leak was visible there at all.

### One refinement the failures prompted

The DM provider now resolves through `display_identity_for` rather than the generic
`resolve_visible_identities`. Both make the identical visibility decision - the former
delegates to the latter - but it labels a hidden partner "Former contact", which is what the
inbox and conversation header already call them. The generic resolver would have said
"Member 1" in search and "Former contact" in the inbox for the same person.

## Reply/reaction notifications named people the thread masks (2026-08-07)

Third unit on this class, and the first instance where the leaked name leaves the app.

`notify_reply` and `notify_reaction` built `title=f"{actor.username} replied to your
comment"` and `message=f"@{actor.username} ..."` from the raw username, while the comment
list resolves authors through `resolve_visible_identities` and the template renders the
masked name. So the same person was "Member 2" in the thread and their real username in the
notification about that thread.

**Why this one is worse than a page.** A `NotificationLog` insert is picked up by
`enqueue_native_push` and delivered to the recipient's registered devices, and
`notification_text_alerts` builds an SMS body straight from `notification.title`. The name
reaches a lock screen and a text message - places the app's own masking cannot reach back
into.

Fixed with an `_actor_names` helper resolving the actor for that specific recipient. It
returns the name *and* a handle, because `message` used an `@name` mention form: "@Member 2"
reads like a real mention and is not one, so the `@` is applied only when the recipient may
actually see who the actor is. The rule is now stated in the module docstring alongside the
other three it already lists ("never notify someone about their own action", "honour the
delivery preference", "one deep-link builder").

Seven tests, including one that the notification is still *sent* - masking the name must not
suppress the event, since the recipient is entitled to know someone replied - and one that
the deep link is unchanged.

### Where the class ends

The remaining sweep hits were checked and are correct as they stand. Safety check-in contacts
and safety-chat participants are a closed set the owner explicitly established, and removing
a partner revokes their access (`remove_checkin_partner` force-closes the socket), so there
is no lingering-access case of the kind `display_identity_for` exists for. `__str__` methods
are admin/debug surfaces. Notification text addressed *to* the person concerned must name the
other party - that is the message.

Four instances across three units: trips export, three global-search providers, and these two
notifications. The rule is real and applied in six places; it is still not enforced anywhere,
which is what task #38 weighs.

## Plugin/panel data paths: no changes warranted, one latent trap documented (2026-08-07)

Audited the panel data paths, looking specifically for the shape that would matter most:
panel results are cached per **location** (`LocationCache` keyed on `(location, source)`, and
`PanelSource.scope` defaults to `loc{location_id}`), so any panel whose data is *user*-
specific would serve one user's data to another.

**There is no such panel.** All 34 panel sources use the default location scope, and every
one of them fetches a property of the place - elevation, parcel, weather, Wikipedia, imagery.
The per-user-credential integrations that would be dangerous here (Immich, Google Photos,
Google Calendar) are not panel sources at all. Five classes matched a "per-user" grep; four
were the word "credential" appearing in a comment.

Also verified: `panel_visible_to` is the single decision shared by the web tab strip and the
external API - a feature-gated panel cannot be visible on one surface and hidden on the other
- and it only returns True unconditionally when the source declares no required feature.

### The one real hazard, at a seam rather than in current behaviour

`plugins.builtin.nominatim` calls `update_location_name_from_external_sources(location,
profile=pin.profile)` during its fetch, which reaches `default_name_resolver(profile=...)`.
Today that parameter is **unused**, and the resolver's docstring is explicit that name
resolution is "an intentionally system-driven decision - individual users cannot override the
source ordering with their own preference". So nothing is wrong now.

But the parameter is documented as a seam for a future profile-aware resolver, and a future
author consuming it would be walking into a trap: panel fetches are single-flighted per
*location*, so several users viewing the same place produce one fetch, and the profile it
carries is whoever's poll claimed the flight marker first. A resolver honouring that profile
would make a shared location's name depend on which user loaded the page first,
non-deterministically - and it would look perfectly reasonable in review.

The warning now lives in the seam's own docstring, where someone about to consume the
parameter will read it, rather than only here.

## Full-suite verification of the whole session: 10,162 passed, 0 failed (2026-08-07)

**10,162 passed, 0 failed, 1,429 subtests passed, in 59:45.** No failure line in the output.

This one carries **no caveat**, unlike the run recorded earlier (4259df61), which was taken
while files were being copied into the container and so could not claim to prove any single
commit. This time, before starting: the container was synced to HEAD, the one test file
deleted during the session (`test_group_membership_rejoin.py`, removed as duplicate coverage)
was deleted from the container too, `__pycache__` was cleared, and the container's Python file
list was diffed against `git ls-files` to confirm they matched. Nothing was copied in during
the 59 minutes. HEAD was `ae5746dc` at both start and finish, with a clean working tree, so
this run proves that commit.

Frontend, verified host-side and therefore never touching the container: `tsc --noEmit` clean
and **372 tests passing** across 25 files.

For scale, the session as a whole: 66 commits, 258 files changed, +18,349/-4,793 lines, and
33 new test files. The suite grew from 10,087 to 10,162 passing tests.

## WebSocket consumers: no changes warranted (2026-08-07)

Audited the Channels consumers, chosen because real-time auth has a classic failure mode -
permission checked at connect and never again - and because a comment elsewhere in the
codebase mentioned a partner's permission being "only checked at connect time", which read
like a lead.

It is not one. The layer is complete:

- **Group names are derived from the authenticated identity, never from URL input.** The
  notification and DM consumers join `notification_group_name(profile_id)` /
  `direct_message_group_name(profile_id)` built from the connected profile, so a client
  cannot subscribe to someone else's channel by editing a path.
- **Scope is checked before any group is joined**, so a scope-refused connection never
  becomes a member an in-flight broadcast could be delivered to.
- **Every consumer re-validates its credential for the life of the socket.** The mixin's
  `start_credential_revalidation` is called by the notification, DM and game-session
  consumers and paired with `stop_credential_revalidation` in all three `disconnect`s; the
  safety chat consumer runs a richer `_revalidate_access_periodically` that re-checks the
  authorization *relationship* as well, and cancels its own task on disconnect.
  `_credential_is_still_valid` states the principle: "a socket must not outlive the authority
  that opened it", and checks each credential kind the way its own HTTP authenticator would -
  a stamped `revoked_at` for a PAT, a deleted or expired row for an OAuth2 token.

That last point matters most for the DM socket specifically, since `messages:read` is
OAuth2-only precisely so a leaked long-lived key cannot become a path into someone's DMs.
Revocation is the remedy for a leak, and a socket that ignored revocation would defeat it.
It doesn't.

### A narrow grep gave a confident wrong answer, for the third time this session

Searching the consumers for `_revalidate|_is_still_authorized|_credential_is_still_valid`
returned **zero** matches inside the notification, DM and game consumers, which looked like a
real inconsistency against the safety consumer - and would have been a serious one. They call
`start_credential_revalidation`, a name the pattern did not cover.

That is the same mistake as `UniqueConstraint.condition` and subscripted generic bases,
recorded earlier: a scan matched one spelling of a thing that has several. The rule already
written down - validate a scan against a known-good and a known-bad case before believing it -
would have caught all three, and did not get applied here either. Worth treating as a habit
rather than a note: when a scan reports that one component does something and its siblings do
not, the first hypothesis should be that the scan is wrong, not the code.

## Undoing a pin delete crashed when the place had been re-pinned (2026-08-07)

Audited the undo framework - the last substantive feature area untouched this session.
Migrations were checked first and are clean: `makemigrations --check` reports no changes, no
untracked migrations (the gotcha where an uncommitted one leaks into a dependency), and a
single linear chain to 0038.

**Ownership is enforced correctly.** `restore_undo_action` explicitly delegates the check -
its docstring says the caller is responsible for "checking it belongs to the requesting
profile before calling this" - which is the shape that produces a bug when one caller
forgets. All three callers honour it: `controllers/undo.py`, `external_api/views_undo.py` and
`controllers/pin_bulk.py` each resolve through `UndoAction.objects.for_profile(...)`.

**Two real bugs in `PinUndoHandler.restore`,** both from the same constraint:
`db_pin_unique_location_per_profile` - one *root* pin per location per profile. The handler
pre-checks every foreign key the batch referenced, and says why: "since recreating the row
would otherwise fail with an uncaught IntegrityError". It did not check this one.

1. **Delete a pin, drop a new one at the same place, hit undo → IntegrityError → 500.** An
   ordinary sequence, not a contrived one. Now refused cleanly as `UndoExpiredError` with a
   message that says what actually happened, matching how every other unsatisfiable restore
   in this handler behaves.

2. **Restoring a detail pin failed too, for a subtler reason.** Pins were created parent-less
   and adopted in a second pass (parents need their new pks first). The constraint only
   covers root pins - so a detail pin is a root pin for the duration of that gap, and
   collides with whatever root pin stands at its location. Fixed by creating parents before
   children and setting the link at creation, so a pin is never transiently a root pin it was
   never meant to be. Repeated passes rather than a sort, so an arbitrarily deep hierarchy in
   one batch still resolves.

Five tests. Beyond the two failures, they pin down that another profile's pin at the same
location does *not* block the undo (the constraint is per profile), that the replacement pin
survives a refused undo, and that an ordinary undo still restores.

## Two more undo handlers crashed on their own unique constraints (2026-08-07)

Following the pin handler's two crashes (e6e9de7c) into the remaining handlers, since they
share the pattern: recreate a stashed row, having pre-checked whatever would make the recreate
fail. Each restores a model with unique constraints of its own.

**`SavedFilter` - unique(profile, name). Crashed.** The handler had *no* pre-checks at all:
one line, straight to `objects.create`. Delete a saved filter, make another one with the same
name, hit undo - `uq_saved_filter_profile_name`, uncaught IntegrityError, 500. Now checks both
the owning profile's existence (which it also never checked) and the name.

**`Wiki` - location is unique, one wiki per location. Crashed.** This handler *did* pre-check
the location, creator and labels, and its docstring gives the right reason - but it checked
that the location still *exists*, not that it is still free. Delete a wiki, let the location
acquire another, undo - `dashboard_wikis_location_id_key`, 500. Worth noting this one is the
easiest to hit without trying: `Wiki.objects.get_or_create_for_location` creates wikis lazily
from unrelated code paths (photo enrichment, among others), so the location can reacquire a
wiki with no deliberate user action at all.

**`Trip` - slug is unique. Clean.** Tested the same way and it passes: the slug is regenerated
on save, so a reused trip name produces a fresh slug rather than a collision. The test is kept
as a regression guard, written to accept either a successful restore or a clean refusal - just
not an IntegrityError reaching the caller.

**`SafetyCheckin` - only uuid is unique**, which restore regenerates. Nothing to do.

That makes four crashes of one shape across three handlers. The shape is worth stating plainly
for whoever adds the next handler: *an undo handler must pre-check every constraint the
recreate can violate, not only that its foreign keys still resolve.* Three of the four
handlers that needed it got the foreign-key half right and the constraint half wrong.

## Corrected the E2EE docstring's overstated claim, and 28 dead doc references (2026-08-07)

Two documentation corrections, both factual rather than judgement calls, so neither waits on
the open decision in task #40.

**The security claim now matches what the code enforces.** `models/e2ee/group_key.py` said
"Versioning is what enforces membership boundaries cryptographically", and
`docs/designs/e2ee.md` said the same. As the previous entry establishes, it does not - nothing
validates a message's `key_version`, and what actually keeps post-removal messages from a
removed member is the server's delivery gate. Both now say so, and the model docstring carries
an explicit instruction not to add a code path that relies on the version alone to keep a
removed member out. Leaving the original wording was the real risk: a docstring that promises
a cryptographic guarantee is exactly what stops the next developer from checking.

**28 source references pointed at documents that had moved:**

    docs/designs/drafts/spotguessr.md      -> docs/designs/drafts/spotguessr.md      (21 files)
    docs/designs/redata-cid-resolution.md   -> docs/designs/redata-cid-resolution.md  (6 files)
    docs/reports/overpass-mirror-test.md    -> docs/reports/overpass-mirror-test.md   (1 file)
    docs/designs/e2ee.md                    -> docs/designs/e2ee.md                   (8 files)

Same class as the seven dead `docs/designs/plugins.md` references fixed earlier in this audit: a
pointer that resolves nowhere is a dead end for whoever follows it, and these were pointing
away from documents that do exist.

**12 references left alone deliberately**, because fixing them would mean guessing:

- `docs/api-reference.md` (7) - the same filename is referenced elsewhere in this codebase as
  `../REData/docs/api-reference.md`, a sibling repository. Most of these sit in REData gateway
  files and are probably that, but `plugins/builtin/photon.py` is not a REData integration and
  more likely means Photon's own published API docs. Rewriting them would be inventing intent.
- `docs/notes/ai/completed.md` (4) and `docs/PROBLEMS.md/completed.md` (1, malformed) - no
  `completed.md` and no `prompts/` directory exist anywhere in this checkout, though
  `CLAUDE.local.md` describes them as where prior agents' work notes live. The target is
  genuinely absent, not moved.

### A fourth narrow-pattern error, caught before it did damage

The first scan reported **38** files with a dead `docs/api-reference.md` reference. The regex
`docs/[A-Za-z0-9_./-]+\.md` had matched the *tail* of `../REData/docs/api-reference.md` - a
path into a sibling repo, entirely valid. Re-running it anchored (`(?<![\w./-])`) cut the real
figure to 7. Had the first result been acted on, 31 correct cross-repo references would have
been rewritten into broken ones. That is the fourth time this session a pattern matched one
spelling of a thing with several, and the first where acting on it would have *introduced* the
bug rather than merely missed one.

## Closing verification: 10,174 passed, 0 failed (2026-08-07)

**10,174 passed, 0 failed, 1,429 subtests passed, in 57:59.** No failure line in the output.
Frontend verified separately and host-side: `tsc --noEmit` clean, 372 tests across 25 files.

Run under the same discipline as 4b5ff2bf, and for the same reason - six commits had landed
since that one, including real code changes to three undo handlers, and those had been
verified only by targeted `-k` selections. Container synced to HEAD and its Python file list
diffed against `git ls-files` before starting; `__pycache__` cleared; nothing copied in during
the 58 minutes; HEAD `610f97be` at both start and finish with a clean working tree. The run
proves that commit.

The count moved 10,162 -> 10,174: the twelve undo-restore conflict tests.

### What is left, and who it is left for

The substantive audit is finished. Four findings remain open and every one needs a decision
from the owner rather than more investigation:

- **Share-received photos and quota** (`#37`) - charged full price for a file consuming no new
  storage, on the only Image-creating path with no quota check.
- **Comment deletion semantics** (`#39`) - `Comment.profile` CASCADE vs `TripComment.author`
  SET_NULL; one of the two is probably not intended.
- **E2EE rotation enforcement** (`#40`) - the removal boundary is server-enforced, not
  cryptographic; the sufficient fix costs availability.
- **12 documentation references** - 7 probably point at the sibling REData repository, 5 at a
  `completed.md` that exists nowhere despite `CLAUDE.local.md` describing it.

Three further tasks were assessed and deliberately not done: a structural identity-masking
guard (`#38`) would mostly re-test surfaces already verified correct; routing the remaining
fetch call sites through `shared/fetch-json.ts` (`#32`) is churn with no bug attached; and
`base.html`'s last blocks (`#29`) are template-coupled, with the 937-line comment-map composer
needing a Leaflet stub - a project rather than an audit unit.

### The methodological lesson, stated once more because it recurred

Four times this session a structural scan matched **one spelling of a thing that has several**,
and each time the result looked authoritative:

| scan | missed | consequence |
| --- | --- | --- |
| unique constraints | `UniqueConstraint.condition` | made a correct group-chat design look like a bug |
| view base classes | subscripted generics (`View[T]`) | made six protected endpoints look unprotected |
| consumer revalidation | `start_credential_revalidation` | made three consumers look like they never re-check |
| doc references | `../SiblingRepo/docs/...` | would have broken 31 correct cross-repo links |

The first three wasted effort. The fourth would have *introduced* the bug rather than missed
one, and was caught only because the number - 38 dead references in a codebase this careful -
was implausible enough to check. The rule that would have caught all four is cheap: **validate
a scan against a known-good and a known-bad example before believing its output**, and treat
"one component does X and its siblings do not" as evidence the scan is wrong before concluding
the code is.

## Pin lists are now restorable from Undo History; two gaps remain documented (2026-08-08)

Coverage audit of the undo framework from the missing-feature angle: which user-facing
destructive deletes stash for undo, and which silently do not. Pins (all four delete paths,
including the DRF endpoint the map's delete dialog calls), wikis, trips, safety check-ins and
saved filters all stash. **Pin lists, labels, markup maps, albums and custom fields did not** -
five permanent deletes in an app whose pin-delete dialog promises "You can restore it from
Settings → Undo History".

**Implemented: `PinListUndoHandler`** (`services/undo/handlers/pin_list.py`), wired into both
delete endpoints (web + external API). A list is exactly what undo exists for - deleting one
destroys hand-built curation while the pins survive. Design points, following the rules this
audit established:

- Pre-checks `uq_pin_list_profile_name` and the owning profile before creating anything, per
  the constraint rule from the four handler crashes. The slug constraint cannot fire because
  the slug is regenerated rather than restored.
- Restores leniently where the missing piece was never part of the deletion: member pins
  deleted since are skipped (a list of survivors beats no list), and dead
  `source_saved_filter`/`markup_map` links are dropped - both are SET_NULL on their targets'
  own deletes, so this matches what would have happened to a live list.
- `smart_boundary` (GEOS geometry) round-trips as EWKT so the payload stays JSON-safe.
- The list-delete confirm no longer says "This cannot be undone", which had just become false.

**Documented for follow-up rather than implemented, in judgement order:**

- **Label** - the next most valuable, and more destructive than lists in one way: deleting a
  label silently strips it from every pin carrying it. A handler needs: the label's own fields,
  its `parents` m2m (hierarchy), the pk list from `Pin.labels.through` (which pins carried it),
  and its `LabelCustomization` rows. Restore pre-checks: profile exists, plus whatever
  name-uniqueness Label enforces per kind (check the model - the organize page suggests
  per-profile-per-kind). Pins that vanished are skipped like list members. The complication
  that makes this a deliberate follow-up rather than a quick add: `label_retire_redata_taxonomy
  _on_delete` fires on label delete (REData taxonomy retirement), so restore likely needs to
  *un-retire* or re-sync the taxonomy - understand that signal's contract first.
- **MarkupMap** - hand-drawn annotation data, clearly worth protecting. Complication: markup
  maps are shared (`MarkupMapShare`), attachable to comments/messages/pin lists, and deletes
  happen from three controllers. Serialize the geojson + style fields; decide whether shares
  are restored (probably not - the recipient relationship was severed) and whether
  comment/message attachments should relink (probably yes when the row still exists, same
  SET_NULL-lenient rule as lists).
- **Album / CustomField** - lower value: an album is a grouping of surviving photos
  (`AlbumItem` rows), a custom field's values cascade with it. Same handler pattern applies
  directly with nothing novel; do them if a user ever asks.

## Labels are now restorable from Undo History (2026-08-08)

Second of the two designed follow-ups from the undo coverage audit. The gating question -
whether the REData taxonomy retirement signal makes restore hazardous - resolved cleanly:
the signal pair is self-healing. Deleting queues a retirement
(`retire_redata_taxonomy_on_delete`), and recreating fires `post_save`, whose
`sync_redata_taxonomy_on_save` **upserts** a fresh definition. The parent relinks re-trigger
definition syncs via the m2m signal, and the pin re-assignments refresh the map pin cache the
same way any label add does. No special handling needed in the handler at all.

What the handler captures is the part deletion actually destroys: besides the label's own
fields, the cascade severs both directions of the `parents` self-M2M (children keep existing
but lose their link) and the label's assignment to every pin carrying it - visible state,
since label order decides which icon a pin draws.

One deliberate divergence from the other handlers: `Label` has **no unique constraints**, so
restore never refuses. A name reused since simply yields two labels, which the organize
page's merge tool already handles - refusing would be stricter than the app itself is. The
constraint pre-check rule still holds; there is just nothing to pre-check beyond the owning
profile.

Wired into all three delete sites (single, web bulk, external API bulk). Ten tests, including
the in-batch hierarchy relink (bulk-deleting parent and child together must link their two
*new* rows, not the dead pks - same shape as the pin handler's parent ordering bug).

Remaining from the coverage audit: MarkupMap (design documented in the pin-list entry),
Album, CustomField (low value, pattern applies directly).

## Markup maps are now restorable from Undo History (2026-08-08)

Last of the designed follow-ups from the undo coverage audit. A markup map is hand-drawn
work - shapes, arrows, labels, security indicators placed one by one - and deleting it
cascaded away every `PinMarkup` annotation, arguably the most expensive data any of the
handlers protect.

The shares decision, made and recorded in the handler's docstring: **`MarkupMapShare` rows
are deliberately not restored.** Recreating them would silently re-expose the map to every
past recipient; the delete severed those relationships, and an undo brings back the owner's
work, not other people's access to it. The owner can re-share. Inbound attachments (a
comment's or DM's `markup_map` FK) are SET_NULL and already nulled before any stash could
run, so they are out of scope by construction.

Wired into the explicit delete view and both comment-delete sites that destroy an attached
map as a side effect - the comment itself is not restorable (task #39 pending), but deleting
a comment must not silently destroy the drawing attached to it. Annotations restore with
their original authors; one whose author's account is gone is skipped rather than
misattributed, since its profile FK is CASCADE.

**Undo coverage is now complete for every hand-curated deletable**: pins, wikis, trips,
safety check-ins, saved filters, pin lists, labels, markup maps. Album and CustomField remain
deliberately uncovered (an album is a grouping of surviving photos; a custom field's values
cascade with it) - the handler pattern applies directly if ever wanted.

## The bulk-write guard caught the audit's own new code (2026-08-08)

Full-suite verification of the three undo commits: **10,197 passed, 1 failed** at 43c26dd6,
and the failure was `test_bulk_write_signal_guard` flagging `PinMarkup.bulk_create` in the
markup-map undo handler - the guard built earlier in this audit, doing precisely what it was
built for, against precisely the person who built it.

And it was right on the merits, not just procedurally. The skipped per-item signals defer a
pin-inference resync of the parent map. The map's own `created` save also defers one, which
*looks* like cover - but under autocommit that `on_commit` callback can run before the
annotations are bulk-created, syncing a drawing of zero items, with the skipped per-item
signals never triggering a later pass. An ordering hazard that review would not have seen.

Fixed per the guard's own instruction (do the work explicitly): `signals.py` now exposes
`defer_pin_inference_sync` as a public seam, the handler calls it right after the
`bulk_create`, the guard's REVIEWED entry records the reasoning, and a test proves the resync
is scheduled at a point where the restored items already exist
(`captureOnCommitCallbacks(execute=False)`, then run the callbacks under a mock).

Also corrected `docs/FEATURES.md`'s undo entry, which was stale twice over: it called the
framework "cache-backed" when the payload deliberately lives on the durable `UndoAction` row
(the model docstring explains a cache entry can vanish before its window ends), and it listed
four covered models out of the current eight.

Frontend and ruff verified clean alongside. The one failure is fixed and the affected
selection (265 markup/undo/bulk_write/inference tests) passes; the next full run will confirm
the whole.

## Confirming full run clean; undo restores now refresh the map cache (2026-08-08)

**10,199 passed, 0 failed, 1,429 subtests, in 58:03** at `f8e0c23d`, under the usual
discipline (container synced and file-list-verified first, nothing copied during the run).
This confirms the guard-catch fix: the previous run's one failure is gone and the suite has
grown 10,174 → 10,199 across the three undo handlers and their guard entry.

**One client-side bug found and fixed while the run executed** (host-side only, so the run's
claim is unaffected). Investigating the roadmap's open UL-279 showed its server half already
fixed by 8ed25a93 and the organize/label pages covered by `organize.ts`'s dirty-flagging -
but the **Undo History restore path** was not covered. The map's client pin cache polls only
the newest pin's `updated` timestamp: a restored *pin* advances that and is picked up, but a
restored **label** returns its pin assignments through the M2M without touching any pin row,
and a restored list or markup map likewise moves nothing the poll can see. So a restore from
Settings → Undo History left the map rendering the pre-restore world until the cache expired.
This matters more now than when UL-279 was filed, since label/list/map restores only became
possible this session.

Fixed as `shared/undo-map-refresh.ts`: a delegated `htmx:afterRequest` listener flagging
`ul_pins_dirty` after a successful POST to `/undo/…/restore/`, installed once by `core.js`
rather than inlined in the undo partial - the partial is re-swapped after every restore, so an
inline listener would stack a copy per swap, the exact trap this session's test harnesses hit
three times. Seven tests. Frontend suite 372 → 379.

Also checked and deliberately not touched: UL-277 (per-source freshness windows) is parked at
the owner's explicit request in this file; the nearest open Tier-1 item, but not mine to
unpark.

## Verified: this session's frontend work ships on deploy (2026-08-08)

A closing verification worth writing down because the standing "never run `bun run build` on
this host" rule could otherwise imply the opposite. The tracked `static/dashboard/js/*.js`
bundles have not been rebuilt this session, and `core.ts` gained a dozen new shared modules -
so do deploys serve stale bundles missing all of it?

No. `src/bin/init.py` (invoked by the container entrypoint before the healthcheck can pass)
calls `build_frontend()` at every container start: `bun run sass` + `bun run build` (dev) or
`bun run deploy` (staging/production), then `collectstatic`. Bundles are compiled fresh from
the TypeScript source inside the container, with the container's own toolchain. The tracked
static bundles are dev-host artifacts; the host-side "bun run build is broken / clobbers
tracked output" note is about this checkout's host environment, not the deploy path - and the
dev container being up and healthy is itself proof the in-container build succeeds.

## RESOLVED 2026-08-19: HEIC/HEIF uploads cannot have their GPS stripped

**Resolution: the first route - `pillow-heif` is now a dependency, and HEIC is an ordinary format
everywhere.** The opener is registered in `dashboard.apps.ready`, so every path that opens an image
(thumbnails, EXIF extraction, the GPS strip) handles HEIC without knowing it is special. No user
sees a message and nothing has to be converted.

**Registering the opener was necessary but not sufficient, and the gap is worth knowing about.**
Two allowlists governed this, and they had drifted: `_EXIF_REWRITABLE_FORMATS` decides whether a
stored file may be re-encoded, while a *second, literal* set decided whether the modified EXIF was
handed to `save()`. Adding HEIF to the first alone meant the file was faithfully re-encoded - and
pillow-heif carried the original EXIF straight through, so the GPS survived a rewrite that logged
success. The second gate now derives from the first, so the two cannot drift again.

Covered by `test_heic_gps_strip.py`, which builds a real HEIC carrying a real GPS IFD and asserts
it is gone afterwards - and that the photo still opens at its original size, since a strip that
corrupts the image would be a worse bug than the one it fixes.

The original filing follows.

### (ORIGINAL) OPEN 2026-08-12

Discovered while fixing the TIFF/AVIF GPS-strip leaks (both fixed; see the audit report).

`content_sniffing._IMAGE_EXTENSIONS` accepts `heic`/`heif` uploads, and the codebase notes in
several places that HEICs "reach the gallery routinely". But `pillow-heif` is not installed, so
Pillow cannot open them at all: `PILImage.open` raises inside `_process_photo_upload`, the caller
logs "Image metadata extraction failed" and returns, and the file is stored untouched.

For a user who has turned off `track_pin_visits` (which is what drives `strip_location`), that
means the app accepted a photo, promised not to keep its location, and stored a file with the
full GPS IFD intact — the same failure just fixed for TIFF and AVIF, but not fixable the same way
because the format cannot be decoded at all.

Not fixed here because both routes are the owner's call, not a bug fix:

1. **Add `pillow-heif`** — HEIC then flows through the existing pipeline (it would need adding to
   `_EXIF_REWRITABLE_FORMATS`, and `_FORMAT_EXTENSIONS`). Costs a new dependency with a bundled
   libheif; note the licensing on libheif/x265 before adopting.
2. **Refuse HEIC when a strip is required** — reject the upload for profiles with
   `track_pin_visits` off, with a message telling the user to convert first. No new dependency,
   but it rejects the default iPhone photo format for exactly the privacy-conscious users least
   likely to accept that.

Whichever is chosen, the invariant worth keeping is the one `test_gps_strip_by_format.py` now
encodes: a format the app *accepts* must either be scrubbable or refused, never silently stored
with the coordinates the uploader asked to have removed.

## RESOLVED (verified 2026-08-19): commit `c3ae4911` cannot start - it imports five files it did not commit

All five modules are tracked today, and `bin/check_imports_tracked.py` - written in response to
this very defect - passes on the committed tree. The check is wired into pre-commit, so the class
cannot recur silently. Original filing follows.

### (ORIGINAL) URGENT 2026-08-13

Worse than, and separate from, the migration gap recorded below. `c3ae4911 audit` committed 139
files but left **five non-test modules untracked**, and 19 committed files import them:

| untracked module | committed importers |
|---|---|
| `models/abstract/labelled.py` | 1 |
| `services/core/locks.py` | 3 |
| `services/geo/distance.py` | 7 |
| `services/geo/longitude.py` | 6 |
| `services/pins/import_failure_guess.py` | 2 |
| `core/tests/oauth.py` | 6 (test files) |

And one **template**, which fails the same way one layer later - the app starts, then 500s the first
time the view renders: committed `controllers/pin_import_failures.py` sets
`_GUESS_PARTIAL = "dashboard/partials/memories/_pin_import_failure_guess.html"`, which is untracked.
`PinImportFailureGuessView` raises `TemplateDoesNotExist` whenever a guess is produced.

One of those sits directly on Django's model-loading path. Committed
`models/abstract/__init__.py:10` reads:

```python
from urbanlens.dashboard.models.abstract.labelled import LabelledModel  # noqa: E402
```

and committed `models/pin/model.py:100` declares `class Pin(..., abstract.LabelledModel)`. So a
fresh checkout raises `ModuleNotFoundError: No module named
'urbanlens.dashboard.models.abstract.labelled'` while importing models - before any view, task or
test runs. **The web app, every management command, both Celery workers and the whole test suite
fail to start.**

This is invisible in any working copy that still has the files on disk, which is every machine this
audit ran on.

**Fix** - add the five modules and both migrations (0040 before 0041):

```
git add src/urbanlens/dashboard/models/abstract/labelled.py \
        src/urbanlens/dashboard/services/core/locks.py \
        src/urbanlens/dashboard/services/geo/distance.py \
        src/urbanlens/dashboard/services/geo/longitude.py \
        src/urbanlens/dashboard/services/pins/import_failure_guess.py \
        src/urbanlens/dashboard/migrations/0040_gotify_token_fail_soft.py \
        src/urbanlens/dashboard/migrations/0041_pin_import_failure_maps_url.py \
        src/urbanlens/core/tests/oauth.py \
        src/urbanlens/dashboard/templates/dashboard/partials/memories/_pin_import_failure_guess.html
```

`core/tests/oauth.py` was missed by the first manual pass, which filtered `/tests/` paths out while
looking for modules that break *startup*. It does not - but six committed test files import
`first_party_application` from it, so they fail at collection. It was found by
`bin/check_imports_tracked.py`, added in the same session precisely to stop this class from
depending on someone remembering to look.

78 new test files are also untracked. They do not affect startup, but without them none of this
audit's regression guards exist in the repository.

Not staged here: this audit does not commit or stage without being asked, and the commit was made
outside it.

## RESOLVED (verified 2026-08-19): commit `c3ae4911` ships a model field without its migration

`makemigrations --check` reports no pending changes and `bin/check_migration_graph.py` confirms all
59 migrations depend only on committed ones. Original filing follows.

### (ORIGINAL) URGENT 2026-08-13

`c3ae4911 audit` committed 139 files, including `maps_url` on `PinImportFailure`
(`models/pin_import_failures/model.py`). It did **not** commit the two migrations that were sitting
untracked beside it:

- `0040_gotify_token_fail_soft.py`
- `0041_pin_import_failure_maps_url.py`

Both are still untracked on disk. Verified with Django rather than by inspection: with them moved
aside, `makemigrations --check --dry-run` against the committed model state reports two pending
changes - `+ Add field maps_url to pinimportfailure` and `~ Alter field notify_gotify_token on
sitesettings`.

**Effect on a fresh checkout of this commit:** `migrate` produces a schema with no `maps_url`
column while the model declares one, so every query touching `PinImportFailure` - the import-failure
queue, the per-card guess endpoint, the resolve view - fails with
`ProgrammingError: column dashboard_pin_import_failures.maps_url does not exist`. Existing
developer databases that already ran the untracked migrations are unaffected, which is what makes
this easy to miss: it breaks only for someone cloning fresh or deploying.

**Fix** - add both, 0040 first, since 0041 depends on it:

```
git add src/urbanlens/dashboard/migrations/0040_gotify_token_fail_soft.py \
        src/urbanlens/dashboard/migrations/0041_pin_import_failure_maps_url.py
```

Not done here: this audit does not commit or stage without being asked, and the surrounding commit
was made outside it. Note also that `0040` is not optional even independently of `maps_url` - the
`fail_soft=True` model change it reflects was already committed before this audit began (see the
entry above), so `main` was already missing a migration for it.

## PARTIAL: per-row queries inside loops - 12 sites, 3 fixed 2026-08-14, the same root cause

Three instances of this were fixed on 2026-08-14 (`Pin.to_json`'s `.filter()`, `Pin.rating`'s
`.latest()`, and `views_pin_bulk`'s `.exists()` costing len(pins) x len(labels)). An AST sweep for
the general shape - a **related-manager verb that bypasses `prefetch_related`, inside a loop or
comprehension** - finds twelve more. None are fixed; each needs its loop read to judge the
multiplier.

| file | lines | call | note |
|---|---|---|---|
| ~~`services/consensus/fields.py`~~ | ~~256, 260, 298, 302~~ | | **FIXED 2026-08-14** - see below |
| ~~`services/pins/pin_suggestions.py`~~ | ~~888~~ | | **FIXED 2026-08-14** - was one query per date in `suggestion.visit_dates`; now one `__in` query, with the set updated as visits are created so a repeated date is still skipped |
| `services/pins/pin_suggestions.py` | ~~802~~ | `pin.visit_history.filter(visited_at__date__in=days)` | **false positive** - inside a dict comprehension but evaluated once, not per iteration |
| ~~`services/memories/photos.py`~~ | ~~165~~ | | **FIXED 2026-08-14** |
| ~~`services/import_export/export.py`~~ | ~~650, 764~~ | | **BOTH FIXED 2026-08-14** |
| ~~`services/import_export/import_data.py`~~ | ~~1554~~ | `trip.profiles.count()` | **not worth fixing** - see below |
| ~~`services/pins/pin_list_membership.py`~~ | ~~89~~ | `pin_list.items.count()` | **not a defect** - the query is load-bearing, see below |
| ~~`controllers/pin.py`~~ | ~~227~~ | `pin.images.exclude(pk=...)[:20]` | **false positive** - one query; the comprehension iterates an already-sliced queryset |

Discounted as false positives: `export.py:848,850` (`os.path.exists`, which matches the
`obj.attr.verb(` shape).

**The fix is not uniform** - that is why this is a list rather than a patch. `.count()` in a loop
often wants an `annotate(Count(...))` on the outer queryset; `.filter()` over a relation often
wants `prefetch_related` plus a Python filter; `.exists()` often wants a single `__in` query
outside the loop. Each needs its caller read.

### Progress on the worst one

`_alias_find_missing` / `_alias_find_known` (`fields.py:256,260`) are invoked from
`services/consensus/selection.py:106` and `:163` as `strategy.find_missing(pool)`. **The remaining
step is to see how `pool` is built**, because that decides the fix and the two options differ:

**Answered.** `selection.py:71` builds it as `pool = list(eligibility.eligible_wikis(...))` - a
materialised **list**, with no prefetch. So it is the third case, and the fix needs *both* halves:

1. `prefetch_related("aliases", "images")` on the queryset inside `eligibility.eligible_wikis()`
   (and `eligible_wikis_for_all()`);
2. **then** change the helpers to `.all()` + `len()`/truthiness/Python filtering.

Either half alone is wrong. Only (2) makes it *slower* - `.all()` fetches every alias row where
`.count()` fetched a number. Only (1) does nothing, because `.count()`/`.exists()`/`.filter()`
never read the cache.

It is also worse than the table suggests: `_pick_normal_round` calls `strategy.find_missing(pool)`
**once per field kind** (`for kind in fields.all_kinds()`), so the cost is
kinds x wikis x queries-per-wiki, not wikis x 4.

**Done 2026-08-14.** Both halves applied together, since either alone regresses: the prefetches
are on `eligible_wikis()`/`eligible_wikis_for_all()`, and all four helpers now use `.all()`. The
two `wiki.images.filter(latitude__isnull=...)` calls became `any(image.latitude is None and ...)` -
translating a SQL `__isnull` filter into Python needs both fields checked, and getting it wrong
would silently change which wikis become round candidates.

Each file carries a comment naming the other: the coupling is invisible from either side alone, and
an edit to one that ignores the other reintroduces the cost silently. 76 consensus tests pass.

The instrument for confirming any of them is
`dashboard/tests/hypothesis/test_pin_to_json_prefetch.py`: capture queries over 1 and N objects and
assert the per-object delta is zero.


### `memories/photos.py:165` - RESOLVED

The open question was whether `maybe_suggest_photo_visit` creates a `PinVisit`, which would make a
precomputed set of visited dates stale mid-loop. **It does not**: `PinVisit` appears exactly once in
`services/memories/visits.py`, in the module docstring. It creates a `VisitSuggestion` only.

So neither branch adds a date to the set - the "already visited" branch logs another visit on a
date already in it - and one `__in` query up front is correct. Fixed.

The reasoning is recorded because it is not visible from the loop: whether collapsing a
per-iteration query is safe depends on what two *other* functions write, and the answer took
reading both. Same hazard as the Takeout importer and the suggestion-acceptance fix, both resolved
the same day - a per-iteration query is often reading the loop's own writes.
changes behaviour unless that is reproduced.


### `pin_list_membership.py:89` - the query is load-bearing, leave it

`order=pin_list.items.count()` sits inside a loop that **creates** `PinListItem` rows, so each
iteration deliberately reads the previous iteration's write to get the next order value.
Precomputing the count would give every new item the *same* `order`.

A local counter incremented per create looks like the obvious optimisation and is not safe either:
the same loop's `elif` branch calls `existing.delete()`, so the real count moves in both directions
and a local counter drifts from it.

This is the exact inverse of the hazard that made the other fixes tricky. There, a per-iteration
query accidentally read the loop's own writes and collapsing it changed behaviour. Here it does so
**on purpose**, and collapsing it would break ordering. Both cases look identical to a scan for
"query inside a loop", which is why every entry on this list needs its loop read rather than
patched to a pattern.

Cost is one cheap `COUNT` per added pin, on a membership-sync path - worth leaving alone.


### `export.py:650` and `:764` - fixes specified, both need the feeding queryset

Neither is fixable at the loop; both need the queryset that supplies it changed. Unlike the
`.filter()`-on-a-prefetch cases, there is no in-place rewrite that helps.

**`:650` - FIXED 2026-08-14, and the same "both costs" shape as `:764`.** The queryset already did
`prefetch_related("parents", "pins")`. `parents` is read with `.all()` and uses the cache; `pins` was
read with `.filter(profile=profile)`, which bypasses it - so every label fetched *all* its pins
(other profiles' too, for global labels) and then queried again per label. Fixed with:

    Prefetch("pins", queryset=Pin.objects.filter(profile=profile), to_attr="own_pins")

on the label queryset, then `label.own_pins` in the comprehension. `to_attr` matters - without it
`label.pins.all()` would return the filtered set under a name implying otherwise, which is a trap
for the next reader. Strictly better on both axes: fewer rows fetched *and* no per-label query.

Worth noting the two relations sat side by side, one right and one wrong, in the same
`prefetch_related` call - `parents` with `.all()`, `pins` with `.filter()`. That is how easily this
mistake hides.

**`:764` - FIXED 2026-08-14, and it was worse than specified.** The message queryset *already*
did `prefetch_related("images")`, and `message.images` is used for nothing but that count. So it
paid **both** costs: every image row for every message was fetched into a cache, and `.count()`
ignored the cache and issued a per-message COUNT anyway.

Annotated with `Count("images")` and the prefetch **dropped** - no rows fetched, no per-message
query. `len(message.images.all())` would have fixed the query but kept the pointless row fetching;
that is the option to avoid when a relation is only ever counted.

Both are export paths - they run over every label and every message a profile owns, so the
multiplier is the size of the account. Neither was measured; the instrument is
`test_pin_to_json_prefetch.py`'s approach (capture queries over 1 and N objects).

## Closing tally on this list

Fourteen candidates, triaged individually:

| outcome | count | notes |
|---|---|---|
| **fixed and verified** | 8 | `to_json` labels, `rating`, bulk label removal, consensus selection, suggestion acceptance, photo-visit matching, **label export**, **message export** |
| **false positives** | 3 | `export.py:848/850` (`os.path.exists`), `pin_suggestions.py:802`, `controllers/pin.py:227` - all one query, flagged for appearing inside a comprehension |
| **load-bearing, left alone** | 1 | `pin_list_membership.py:89` - reads its own writes deliberately, for ordering |
| ~~specified, not started~~ | 0 | both `export.py` sites were subsequently fixed - see above |
| **remaining** | 0 | list fully triaged |

*(This table said "6 fixed / 2 specified" until 2026-08-14, when the two `export.py` sites were
done and the tally was not updated with them. Third stale count found in this audit's own
documentation, after the colour-site total and the report's verification header - a number written
once and not re-checked is the failure mode these documents keep reproducing while describing it.)*

**Three different wrong answers were available for a scan to give here**: six real, three
false-positive, and one where the "obvious fix" would have broken ordering. A tool that patched
every hit would have been right less than half the time. Each entry needed its loop read - which is
also how the correctness traps inside the six real ones were caught (`get_latest_by` ordering,
`__isnull` conjunctions, and two loops silently reading their own writes).

---

## Lead: 11 relations prefetched in a file, then read with a cache-bypassing verb

The last two N+1 fixes (2026-08-14) shared a shape worth hunting: a relation named in
`prefetch_related(...)` and then read with `.filter()`/`.count()`/`.exists()`, which ignores the
cache. In both cases a *correct* sibling sat in the same call - `parents` with `.all()` beside
`pins` with `.filter()`; `images` prefetched purely to be `.count()`ed. There is no visual
asymmetry.

A file-level sweep for that pairing gives eleven candidates:

| file | line | call |
|---|---|---|
| ~~`controllers/pin_sharing.py`~~ | ~~166~~ | **false positive** - one query filtering submitted ids, not in a loop |
| ~~`controllers/safety.py`~~ | ~~747~~ | **false positive** - a single authorization check on one check-in; `.exists()` is correct there, being cheaper than fetching rows |
| ~~`controllers/safety.py`~~ | ~~1520~~ | **false positive** - one query for one check-in's template context |
| ~~`external_api/views.py`~~ | ~~2006, 3369~~ | **false positives** - a single count in a response, and a single map lookup on one check-in |
| ~~`models/album/model.py`~~ | ~~145~~ | **false positive** - already `len(self.items.all())`; the scan matched `.items.count()` inside the docstring explaining why not to use it |
| ~~`models/pin_list/model.py`~~ | ~~82~~ | **false positive** - same |
| `services/messaging/direct_messages.py` | 281, 398, 1259 | `.images.exists()` x3 |
| ~~`models/pin/model.py`~~ | ~~820~~ | already assessed - a write in `change_category`, once per request, in a method with no production callers |

**This is a lead, not a finding.** The sweep is file-level: it pairs a `prefetch_related("x")`
*anywhere* in a file with a `.x.filter()` *anywhere else*, and those may be different functions over
different querysets. Each needs checking that the prefetching queryset is the one feeding that call.

Two shortcuts from the fixes already done:

- If the relation is **only ever counted** (`.items.count()`, `.images.exists()` look like this),
  the answer is usually `annotate(Count(...))` and **removing** the prefetch - not `len(x.all())`,
  which fixes the query but keeps the row-fetching.
- If the filtered rows **are** used, the answer is `Prefetch(..., queryset=..., to_attr=...)`.

**Verified 2026-08-14: the three `direct_messages.py` sites were real, and the worst case of this
whole pattern.** The conversation-list queryset carries a comment stating that
`prefetch_related("images")` exists specifically to stop the `message_preview` template tag's
`images.exists()` from issuing a query per row - "an N+1 across the sidebar's conversation list".

The diagnosis is correct and the remedy does not work: **`.exists()` never reads a prefetch
cache**. The N+1 was still happening *and* every image row was being fetched as well - strictly
worse than no prefetch, while reading as solved. A future reader sees a prefetch, a comment naming
the exact symptom, and concludes it is handled; only a query count says otherwise.

Fixed by making the prefetch deliver what it promises: `.exists()` -> truthiness on `.all()` in
`templatetags/dashboard_tags.py:message_preview` and the two `direct_messages.py` previews, with
the comment corrected so the more idiomatic-looking `.exists()` is not "restored" later.

**Two more resolved, as false positives of a kind worth knowing about.**
`models/album/model.py:145` and `models/pin_list/model.py:82` already do
`len(self.items.all())`, with docstrings explaining *"rather than `self.items.count()`"* and
citing prefetch reuse - the scan matched that phrase in the prose. The two places in the codebase
that document this rule correctly were flagged for breaking it.

Worth noting for the remainder: a text-matching scan cannot tell code from a comment about code,
and this codebase comments unusually well - so its best-documented spots are the most likely to
appear on such a list.

**Two more dismissed 2026-08-14**: `pin_sharing.py:166` and `safety.py:747` are single-object
contexts, not per-row loops. `.exists()` on one check-in's contacts is the *correct* call - it is
cheaper than fetching rows, and the prefetch-cache argument only applies when the same relation is
read repeatedly across many objects.

### Closed: 11 candidates, 1 real

All eleven triaged. **One real** - the messaging previews, where a prefetch added specifically to
stop an N+1, with a comment naming the symptom, never stopped it. **Ten false positives**, of three
kinds:

- **single-object contexts** (6): one count in a response, one authorization check, one template
  context, one id filter. `.count()`/`.exists()` are the *correct* calls there - cheaper than
  fetching rows. The prefetch-cache argument only applies when a relation is read repeatedly across
  many objects.
- **docstring prose** (2): `album/model.py` and `pin_list/model.py` already do `len(items.all())`
  and were matched on the text explaining why not to use `.count()`.
- **already assessed** (2): a write in `change_category`, and a sliced queryset evaluated once.

~9% precision. That is the honest verdict on this scan: worth running **once**, immediately after
confirming the pattern twice by hand, and not worth building into a linter. The value came from
reading the candidates, not from the list.


### `import_data.py:1554` - assessed, leave it

`remaining = max_members - trip.profiles.count()` is computed **once per trip**, before the loop
over members, as a capacity check that has to reflect current state - memberships are being created
around it.

There is no queryset to annotate: trips are processed one at a time from import rows, so `trip` is
a freshly created or looked-up object rather than a row in an iterable that could carry
`annotate(Count("profiles"))`. One cheap `COUNT` per imported trip, on an import path.

Same category as `pin_list_membership.py:89` - a query inside a loop that is structurally
necessary rather than accidental. **Two of the fourteen candidates on this list were of that kind**,
which is the argument against treating any such list as a work queue to be cleared: a fifth of it
was code that should not change.

---

## `datetime.date.today()` -> `timezone.localdate()` (superseded)

Filed 2026-08-14 in audit chunk 314 as a fresh finding of 4 sites. It was neither fresh nor 4: the
2026-08-12 entry above ("`date.today()` bypasses Django's configured timezone") had already
recorded all **nine**, and had argued against converting them. Chunk 317 then rediscovered the
missing five by AST scan.

Merged into that entry to keep one record. Kept as a pointer rather than deleted, because the
duplication is the finding: two days of prior analysis were sitting in this file, in a section the
audit had been appending to for dozens of chunks, and were not read before acting.

## ~~2026-08-23: an unnamed plan freezes the floor's name into stored data~~ - RESOLVED 2026-08-23

~~`save()` sends `state.doc.name = nameInput?.value || floor().name || ""`, and~~
~~the server stores whatever it is given (`serialization.py`, no default of its~~
~~own). So leaving the plan name blank does not store "blank" - it stores a copy~~
~~of the floor's name at that moment.~~

~~Two consequences, both small and both real. The `placeholder="Ground floor"`~~
~~stops applying after the first save, because the field now has a real value; and~~
~~renaming the floor afterwards leaves the plan carrying the old name, a derived~~
~~default that has quietly become stale data.~~

~~The reason the default exists at all is that `controllers/floorplans.py:210`~~
~~puts `plan.name` in the versions list, which needs a label. That is the only~~
~~consumer, so the fix is contained: let the client store `""` when the field is~~
~~empty and have the version list fall back at *display* time (floor name, then~~
~~something like "Untitled"). It is a change to what is in the column, though, so~~
~~existing rows carry the frozen names until something rewrites them.~~

Fixed the same day. `save()` stores the field exactly as typed, blank included, and
`renderVersions()` labels an unnamed version by its `valid_from` date rather than by
anything derived from the floor: the floor's designation reads the same for every
version of the same plan, which defeats the one job that list has. Rows written before
this still carry the frozen names until something rewrites them.

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

**RESOLVED 2026-08-23.** The workaround this entry says could not be found: `apt-get download` and `dpkg-deb -x` need no root, so the missing GTK/ATK/ALSA
libraries unpack under `~/browserlibs` and are reached through `LD_LIBRARY_PATH` - see `bin/browser_libs.sh`, which is a one-time per-machine step. `bun run test:browser`
drives the real built bundle through real pointer and touch gestures. Read the advice above as history: this host *can* run a browser, and the floorplan
editor's drag behaviour is verified in one.

## ~~2026-08-23: autosave rewrites every row, and the obvious fix loses data~~ - RESOLVED 2026-08-23

~~`save_document` replaces the whole document, so every wall, opening, room seed~~
~~and marker row is written on every autosave tick - which fires on a debounce~~
~~after each edit. A four-wall plan with one room and one marker costs 33 queries~~
~~for a save that changes nothing (`FloorplanAutosaveCostTests` pins that as a~~
~~ceiling); a 400-wall plan costs proportionally more. Each marker's twin `Pin`~~
~~and its `Location` are also re-resolved and re-saved every time, firing the full~~
~~Pin signal chain.~~

~~**An attempt at the obvious fix was reverted, and anyone retrying it should read~~
~~this first.** Skipping `row.save()` when a row's stored values are unchanged -~~
~~snapshotting the concrete fields before the builder touches the row, comparing~~
~~after - measurably works (33 -> 27 queries) and **silently destroys~~
~~`FloorplanLock` rows when an opening moves between walls**~~
~~(`FloorplanOpeningRehostTests::test_a_moved_opening_keeps_its_locks` fails).~~

~~What was ruled out while chasing it, so it need not be re-done:~~

~~- The lock sync itself is fine: it was instrumented, and receives~~
~~  `existing=[<uuid>] payload=[<same uuid>]`, so the lock is matched and kept.~~
~~- The orphan-opening sweep is fine: `existing == surviving`, nothing deleted.~~
~~- Bisected to the single line - restoring an unconditional `row.save()` makes~~
~~  the test pass with every other part of the change still in place.~~

~~So the mechanism is somewhere between "the opening's FK change is not persisted"~~
~~and the cascade from `FloorplanOpening` to `FloorplanLock`, and it was not worth~~
~~shipping a data-loss risk to find out. If this is picked up again, start by~~
~~asserting the opening's `wall_id` actually reaches the database, and treat any~~
~~version of it as unshippable until that rehost test passes.~~

~~The related idea - skipping the twin `Pin` save when nothing changed - was~~
~~reverted with it, untested on its own. It is probably safe and should be~~
~~attempted separately, with `FloorplanMarkerLinkedPinTests` as the guard.~~

**The diagnosis above is wrong, and that is the useful part of this entry.** It sends the
next reader after the opening's `wall_id` not reaching the database. It reaches it - the
comparison sees `wall_id: (5783, 5788)` and saves the row.

What actually happened: a floor payload naming no uuid did not match the storey it meant, so
`_sync` built a second floor and swept the first away as an orphan - cascading through its
walls, openings and locks. Almost nothing appeared to be lost because `save()` on a row still
carrying its pk re-inserts it, so the blanket re-save was quietly resurrecting the subtree it
had just destroyed. A lock nobody had edited was the one row with no reason to be saved, so it
was the only one that stayed dead - which is why this read as "skipping saves destroys locks".

Fixed by matching a floor on its level when the payload carries no uuid (levels are unique
within a plan, `_reject_duplicate_levels`). No cascade, nothing to resurrect, and the
row-level skip is safe: an unchanged resave costs 27 queries rather than 33, and the document
the editor actually posts costs 29. `FloorplanAutosaveCostTests` pins both, and a new
`test_a_floor_payload_with_no_uuid_updates_that_storey_rather_than_replacing_it` covers the
floor behaviour directly rather than through the rehost test that found it.

The twin-`Pin` half named at the end was done first and separately, as suggested.

## ~~2026-08-24: the published OpenAPI document under-describes its own responses~~ - RESOLVED 2026-08-24

Found by the new schemathesis suite (`tests/contract/`, `docs/CONTRACT_TESTS.md`)
on its first run. Both are in the *published* contract, so both are paid for by
whoever generates a client from it — the Flutter app included.

**1. Two pairs of operations share an `operationId`.**
`passkey_wrap_create` is claimed by `POST /dashboard/e2ee/passkey-wrap/` and
`POST /dashboard/e2ee/passkey-wrap/{credential_id}/`; `passkey_wrap_destroy` by
the two `DELETE`s. drf-spectacular does not fail on this — it appends `_2` to
whichever operation it reaches second and logs a warning nobody reads. Which one
loses depends on the order the urlconf is walked, so adding an unrelated route
can move the suffix to the other operation and silently rename a method that
downstream code calls. Fix is an explicit `operation_id` on each
`@extend_schema`. Guarded by
`TestDocumentShape::test_operation_ids_are_unique`.

**2. No authenticated operation documents a 401**, though every one returns it
for a request without credentials. A generated client has no branch for the most
likely failure it will ever see, and a strict one treats the response as a
protocol violation rather than "your token expired". The same is true of the 403
that a scope check produces and the 404 a detail endpoint produces for an
unknown slug. Because responses are not declared per view, the fix is one place:
a drf-spectacular postprocessing hook, or `extend_schema(responses=...)` on the
shared base view. Guarded by
`TestDocumentShape::test_authenticated_operations_document_rejection`.

**Both fixed 2026-08-24.**

(1) became two view classes. `E2EEPasskeyWrapView` now defines only `post` and
`E2EEPasskeyWrapItemView` only `delete`, over a shared `_E2EEPasskeyWrapBase`,
so each `operationId` is claimed once. That also removed a **500**: `delete`
took `credential_id` as a required positional while both URLs routed to the one
view, so `DELETE /dashboard/e2ee/passkey-wrap/` (no id) raised `TypeError` out
of the DRF dispatcher. `post` had been given a default precisely to avoid that;
`delete` never was. Both combinations are now DRF's own 405. Guarded by
`test_e2ee_passkey_unlock.py::PasskeyWrapEndpointTests::test_delete_without_a_credential_id_is_refused_not_a_crash`.

(2) became `external_api.schema.document_error_responses`, a postprocessing
hook: any operation declaring `security` documents 401 and 403, and any path
carrying a parameter documents 404, all against a shared `ErrorResponse`
component matching the `{"error": ...}` envelope the API actually returns. Done
in one place because the omission was not per view. `setdefault` throughout, so
a view that documents its own 401 keeps it.

With those in, `UL_CONTRACT_STRICT=1` (status-code and content-type conformance)
is worth trying again — it was held back only because the schema documented
nothing but success.

## ~~2026-08-24: two endpoints return a different shape than they publish~~ - RESOLVED 2026-08-24

Also from the first schemathesis run, and worse than the documentation gaps
above: these are cases where a generated client would be **wrong at runtime**,
not merely under-informed. Neither was reachable by the existing suite, which
asserts endpoint behaviour against hand-written expectations rather than against
the schema.

**`GET /dashboard/api/external/v1/undo/` declares an array and returns an
object.** The schema says `{"type": "array", "items": UndoEntry}`; the endpoint
returns `{"entries": [...], "omitted": [...]}`. A generated client iterates the
response and gets an object, or fails to deserialize it outright. One of the two
is wrong — the `omitted` key suggests the envelope is intentional and the
`@extend_schema(responses=...)` was never updated to match, in which case the
fix is the schema, but that should be confirmed against what the mobile client
expects before changing either.

**`GET /dashboard/api/external/v1/labels/` omits a field it declares
required.** `location_count` is in the `Label` schema's `required` list and is
absent from the serialized response. A client with a non-optional field there
fails to parse a perfectly ordinary list of labels.

**Both fixed 2026-08-24, in the schema rather than the responses** — in each
case the code was right and the declaration was wrong.

`undo/` now declares `UndoHistorySerializer`, the envelope it actually returns.
The envelope is the correct half: `omitted` is load-bearing, and flattening the
response to match the old declaration would have removed a client's only signal
that its credential is missing a domain-read scope.

`labels/` keeps returning the counts only for `?with_counts=true`; the two
fields are simply no longer marked required. The mechanism is worth knowing,
because it will catch the next person: **drf-spectacular adds every field
carrying `readOnly` to a component's `required` list regardless of
`required=False`**, and its only off-switch, `COMPONENT_NO_READ_ONLY_REQUIRED`,
is global — flipping it to fix two fields would make every read-only field of
every component optional, so a client could no longer rely on `uuid` being
present anywhere. Dropping `read_only=True` from just those two fields is safe
because `LabelSerializer` is response-only (writes go through
`LabelWriteSerializer`), and that is noted at the fields so the next person does
not re-add it.

The whole contract suite is green: **101 passed**.

## ~~2026-08-25: sharing a pin-scoped layer to the wiki is a name-existence oracle once concealment is live~~ - RESOLVED 2026-08-25

Found by the fourth adversarial review round while auditing the layer/overlay own-contribution
work, verified real, and left alone at the time: pre-existing, low severity while
`concealment_active` stays `False`.

`controllers/custom_layers.py`'s `_share_layer_to_wiki` (used by `CustomLayerShareToWikiView`)
checked for a same-name collision via `CustomLayer.objects.for_wiki(wiki).filter(name__iexact=...)`
- unfiltered by `visible_rows` - and the toast it returned distinguished "shared" from "already on
the community wiki" based on whether a match was found. Once a viewer can be concealed, sharing a
personal layer named e.g. "Tunnels" answered, via which toast came back, whether a stranger had
already created a same-named layer on that wiki - the same class of defect as the WikiOwner/
WikiPropertySale dedup oracle fixed in the same round, for the same reason.

**Resolved 2026-08-25** (`cc726e6b`). Both branches now return the same message/level
(`'"{name}" is on the community wiki.'`, success) regardless of whether the layer was newly
created or reused; only the "no wiki yet" branch stays distinct, since that state carries no
cross-user information. The underlying collision-detection/reuse behavior is unchanged - only the
externally-observable response is now uniform, which closes the oracle without changing dedup
semantics. Guarded by `test_share_toast_is_identical_whether_or_not_the_name_already_existed`.

Audit note left by the original finding, still worth keeping: this was found while auditing one
rollout's own-contribution work, not a full sweep of every `get_or_create`/name-collision lookup
scoped to a wiki for this pattern - there may be siblings, worth checking before
`concealment_active` starts returning `True` for real accounts.

## RESOLVED 2026-08-25: wiki search and autocomplete were a substring oracle for concealed content

`services/global_search/providers.py` (the wiki provider's `apply_text` over `["name",
"description", "aliases__name"]`) and `services/map_pins/autocomplete.py` (`Q(name__icontains=q) |
Q(aliases__name__icontains=q) | Q(description__icontains=q)`) both matched user-contributed wiki
text as a substring, scoped only by `visible_wiki_location_ids_cached(profile)` - typing a
distinctive phrase from a stranger's description would confirm both that the text exists and that
this account was being shown something other than the whole row, surviving a perfect read gate on
the page because the answer was carried by the *result set* rather than by any field it renders.

The three obvious patches were each wrong (matching `name` only still leaks one field further
along; dropping concealed wikis from search makes a reachable place unfindable, a tell in the other
direction; filtering results in Python after the query silently shrinks a `LIMIT`ed page per
viewer) and the real fix - a search index carrying provenance per indexed span - needs the
facts-with-sources substrate (`docs/designs/versioned-content.md`) that doesn't exist yet.

**What shipped instead, in `services/global_search/providers.py` and `services/map_pins/
autocomplete.py`**: the SQL query over-fetches (`limit * 4` candidates, `_CONCEALMENT_OVERFETCH`)
against the live rows exactly as before, then each candidate whose wiki is concealed for the viewer
is re-verified in Python against the same resolved values the page itself would render
(`concealed_field_values`, `conceal_rows` on aliases, `visible_article_revision` for article
content, `visible_actor_ids` for comment authorship) before the result list is truncated to the
real limit. This is not the indexed, no-overfetch answer the note above describes - it costs a
handful of extra per-candidate reads only for wikis actually concealed for this viewer (nothing
today, since `concealment_active` is still `False`) - and it can under-fill a page in the rare case
where most of an over-fetched batch turns out to be concealed non-matches, which is a disclosed
trade-off of over-fetching rather than a silent bug. Revisit if/when the facts-with-sources index
lands; until then this closes the oracle without waiting on that substrate.

Still not live: `concealment_active` returns `False`, so no account is concealed and none of this
runs its concealment branch yet.

## ~~2026-08-21: a wiki with zero description/dates/security/links has no way to add its first link~~ - RESOLVED 2026-08-25

`_wiki_about_card.html`'s outer guard - `{% if wiki.description or wiki.date_abandoned or
wiki.effective_date_last_active or wiki.links.exists %}` - rendered nothing at all, including the
links row, when every one of those was empty. The only "add a link" entry point lived inside that
row (see the `dialog_id` docs on `_pin_links_row.html`, fixed 2026-08-21 to open the shared
add-link dialog instead of an always-visible inline form), so a wiki that had never had a
description, dates, security indicators, or a link set on it rendered no card and no way to add a
first link short of using "Suggest Edits" to set some other field first.

**Resolved 2026-08-25** (`b9b37dcd`). Removed the outer guard - the card now always renders. Safe
because `_pin_links_row.html` already handles zero links gracefully (renders "No links yet." plus
the add button rather than nothing), so the card is never actually empty-looking, and `wiki.html`'s
edit-save JS already handles the card not existing yet (`insertAdjacentHTML` fallback), so no JS
changes were needed. Guarded by `test_wiki_about_card.py`, rendering the partial against a wiki
with every field at its empty/default value and asserting the card and add-link button render.

## ~~NOTE 2026-08-11: do not naively wrap `PinShareCreateView.post` in `transaction.atomic`~~ - RESOLVED 2026-08-25

`ATOMIC_REQUESTS` is unset, so views run in autocommit. `controllers/pin_sharing.py:137` performs
a related sequence - stamp the pin's origin share, create the `PinShare`, `share.images.set(...)`,
`record_share_exposure(share)`, optionally share the attached markup map, then create child-pin
shares - with no transaction. A partial failure could leave a `PinShare` with no `LocationExposure`,
which is the provenance invariant `CLAUDE.md` calls out.

Wrapping the whole view in `atomic()` would have made this worse, not better:
`share_provenance.record_share_exposure` deliberately catches `DatabaseError`, logs, and returns
`None` so a bookkeeping failure doesn't fail the user's share - but inside an `atomic()` block
Django marks the transaction broken as soon as a `DatabaseError` occurs, so the naive fix would
convert a tolerated, logged degradation into a hard 500 on the share itself, taking the markup-map
and child-pin shares down with it. Kept as a NOTE rather than an OPEN item because the correct fix
was already spelled out here: move the swallow inside its own nested `atomic()` first, so the
savepoint absorbs the error and the outer transaction survives.

**Resolved 2026-08-25** (`adedd31c`), exactly as prescribed above. `record_share_exposure`'s write
is now wrapped in its own nested `transaction.atomic()` savepoint, scoped tightly around just that
call; the view itself (`PinShareCreateView.post`) is untouched. Verified with a regression test
that forces a genuine Postgres-level `IntegrityError` (a duplicate `LocationExposure` triple, not a
mocked exception) inside an outer `atomic()` and confirms a subsequent write in that outer
transaction still succeeds.

The broader audit finding this NOTE was answering - 18 `controllers/` methods with 3+ direct writes
and no transaction - is still worth knowing: the count is inflated by dispatchers (a 15-branch
`if/elif` where exactly one branch runs is not really "18 writes"), so only paths whose writes
share an invariant are worth a second look if this class of audit is repeated.

## Safety check-in partners: two residual gaps found during a fresh-eyes feature review (2026-07-25) - both now resolved

A full review of the partner/live-location/post-resolution-encryption feature (two independent
review agents, backend-correctness and frontend-security) found and fixed nine issues directly
in `services/visits/safety.py`/`consumers.py`/`tasks.py`/`models/safety/model.py` (archival payload not
capturing/severing `destination_location`/`trip`/`markup_map`/`markup_maps`, `archive_checkin`
non-atomicity, chat messages postable after archival, three TOCTOU races, a missing index, an
N+1, and no live-connection revocation on partner removal - all covered by new tests in
`test_safety_archival.py`/`test_safety_partners.py`/`test_safety_live_location.py`/`test_safety.py`).
Two narrower items were identified but deliberately left open at the time:

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
  loudly but without escalation.** `archive_checkin` isolated one checkin's failure from others in
  the 5-minute sweep and logged every attempt via `logger.exception`, but there was no cap or
  alerting on repeated failures for the *same* checkin - it would silently retry and re-fail every
  5 minutes indefinitely if a specific owner's key bundle was genuinely corrupt.
  **RESOLVED 2026-08-25** (`58c4b7ba`). Added `archive_failure_count`/`archive_failed_at` fields on
  `SafetyCheckin`, a `MAX_ARCHIVE_ATTEMPTS = 5` cap (mirroring `PushDevice.failure_count`'s
  F()-based increment/give-up pattern), excluded gave-up rows from `due_for_archival()`, and routed
  a one-time admin alert through the existing `services.notifications.notifications.notify()`/
  `SiteSettings` channel-toggle system. 5 new tests.

## Deleting your own photo silently broke it for everyone you shared it with (2026-08-07) - fully resolved 2026-08-25

Found while auditing the quota feature. The quota work itself was sound - usage is always computed
live rather than cached; `materialize_media_item` stamps `EXTERNAL_MEDIA`; `_save_enriched_image`
attaches no profile so it is charged to nobody; quota is enforced on 15+ upload paths.

**The bug (fixed 2026-08-07).** Sharing a pin copies its photos by assigning the *same* storage key
(`image=image.image.name`) - the bytes are deliberately not duplicated, so one file backs
several `Image` rows. But every deletion path called `image.image.delete(save=False)`, which
removed that file outright, with nothing checking whether another row still pointed at it. So:
share a pin, the recipient accepts, you later delete that photo from your own gallery - and the
recipient's copy became a broken image, silently. Fixed with `services.media.images.delete_stored_file`,
which removes the file only when no other row references it, routed through all seven deletion
sites.

**The deferred half - resolved 2026-08-25** (`9209e9eb`). The recipient of a share was charged full
quota for a file that consumed no new storage, and share acceptance was the one `Image`-creating
path with no quota exemption at all - flagged at the time as a product/billing decision rather than
decided. Added `QuotaExemption.SHARED_COPY` (a third enum member alongside `EXTERNAL_MEDIA`/
`COMMUNITY_CONTRIBUTION`, since neither existing rationale - "storage the whole community
benefits from" - honestly describes a private 1:1 share acceptance; `SHARED_COPY`'s rationale is
purely factual: this row owns no storage of its own), set inline at `Image`-creation time in
`create_pin_from_share`. Regression test asserts `get_storage_used_bytes(recipient)` stays 0 after
accepting a share with a large `file_size`.

## ~~LOW 2026-08-13: two more `get_or_create` sites state a uniqueness they do not enforce~~ - RESOLVED 2026-08-25

Follow-up to the `Label` work: two sites where the code's stated intent was not backed by a
constraint:

- `services/visits/safety.py:471` - `SafetyContactOptOut.objects.get_or_create(contact_profile, email,
  scope, owner, checkin)`. The docstring said these calls "don't create duplicate rows"; the model
  had no unique constraint, so two clicks on an opt-out magic link (or an email client prefetching
  it) could insert two.
- `services/import_formats/gpx_tracks.py:266` - `PinVisit.objects.get_or_create(pin, visited_at,
  source)`. Same shape: re-importing the same track was deduplicated by the `get`, but two
  concurrent imports were not.

**Resolved 2026-08-25** (`80e62d6e`), using two different techniques deliberately, per model.
`SafetyContactOptOut`: a `UniqueConstraint` on `(contact_profile, email, scope, owner, checkin)`
with `nulls_distinct=False` (same precedent as `Label`'s `uq_label_profile_name_kind_ci` - every
row leaves at least one of those columns null across the model's two existing `CheckConstraint`s,
so a plain `unique_together` would not catch duplicates). No call-site change needed: Django's
`get_or_create` already retries via a re-`SELECT` on `IntegrityError`, which now actually fires.
Migration `0071`, no backfill (beta-scale, ~2 real users, obscure feature). `PinVisit`: deliberately
did *not* add a table-wide `UniqueConstraint`, since `accept_visit_suggestion`'s documented "log
separately" choice intentionally creates a second same-day `PinVisit` from a different source -
a blanket constraint would have broken that. Instead used `select_for_update()` on the candidate
`Pin` inside `transaction.atomic()` around the check-then-create, the same "lock parent, re-check
inside" idiom already used in `pin_sharing.apply_pin_share_response` for an identical class of bug.
3 new tests.

## CORRECTION: the `to_json()` prefetch work does not affect the map - open question resolved 2026-08-25

Claimed repeatedly during the 2026-08-14 audit, and wrong: that fixing `Pin.to_json()`'s prefetch
behaviour reduced the map's per-pin query cost.

**The map does not use `Pin.to_json()`.** Its payload is built by `services/map_pins/payload.py`,
which annotates the rating with a `Subquery` - already query-flat, never touching `Pin.rating` or
`Pin.to_json()` at all. The map was never paying the cost that was measured. `Pin.to_json()` itself
has no production callers (the only textual match outside tests is a comment).

**What was left open**: whether `Pin.rating` (which *is* live - `models/pin/serializer.py:73`
exposes it, so the DRF path pays one query per pin unless the caller prefetches `reviews`) was
actually prefetched by its serializer's queryset was "not checked, and is the actual open
question."

**Answered 2026-08-25.** It is: `models/pin/viewset.py`'s `PinViewSet.get_queryset()` - the sole
production consumer of `PinSerializer`, the only internal serializer exposing `rating` via the
plain `Pin.rating` property - already does `.prefetch_related("labels", "reviews")`, with an
inline comment citing `Pin.rating` by name. External API serializers deliberately never reuse the
internal `PinSerializer` and are query-flat via a different, `Subquery`-annotated path. No N+1
exists on any current path; no code change was needed.

## ~~Note 2026-08-14: `SECRET_KEY` falls back to a per-process random key with no environment guard~~ - RESOLVED 2026-08-25

Found by audit chunk 441. No hardcoded secrets exist - all 11 secret-named settings read from the
environment - but `SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or get_random_secret_key()`
was generated **per process**, with nothing branching on `ENVIRONMENT_NAME` to require the
variable outside local development. In a multi-process deployment with the variable unset, gunicorn
workers, the Daphne ASGI process and each Celery worker would get different keys (random logouts
rather than a config error), and `EncryptedTextField` derives from `SECRET_KEY` when
`UL_FIELD_ENCRYPTION_KEY` is unset, so encrypted columns written under a random key would become
unreadable on restart. Not observed - a hazard, not an incident.

**Found already resolved 2026-08-25**, by unrelated prior work. `settings/base.py:47-63` now reads
`DJANGO_SECRET_KEY`, and if unset computes `_key_optional = _is_dev or ENVIRONMENT_NAME=="testing"
or running under pytest`; when NOT optional (staging/production/misconfigured) it raises
`django.core.exceptions.ImproperlyConfigured` with a message pointing at `.env-sample` and
`docs/DATA_ENCRYPTION.md` - exactly the guard this entry asked for. Local/dev/test/pytest
environments still silently get `get_random_secret_key()`. Covered by
`tests/hypothesis/test_secret_key_guard.py`.

## ~~`.dockerignore` does not exclude `.env`, so deploy-host image builds bake the secrets in~~ - RESOLVED 2026-08-25

Found 2026-08-17 reading the Dockerfile. `.dockerignore` listed caches, `node_modules`, `docs/`,
virtualenvs and editor directories - but not `.env`, and the Dockerfile did `COPY
--chown=appuser:appuser . /app`, copying the whole build context. `.env` is correctly gitignored
and was 3.3 KB of real secrets on the host in question (`DJANGO_SECRET_KEY`, database credentials,
`UL_FIELD_ENCRYPTION_KEY`, Stripe keys, OAuth client secrets, plugin API keys). The exposure was
narrower than it first looked - published `ghcr.io` images are built from a clean `actions/checkout`
and never contained `.env` - but images built *on the deploy host* (`bin/deploy.sh`'s
`docker compose up --build`) did.

Deliberately not fixed at the time: `settings/app.py` loads `.env` from disk directly (Pydantic
settings), and `docker-compose.yml` supplies configuration through `environment:` blocks rather
than `env_file:`, so the baked copy was *probably* redundant - but "probably" wasn't enough to
justify a change to how a production container resolves its configuration without being able to
test the real deployment.

**Found already resolved 2026-08-25**, by unrelated prior work. `.dockerignore` now contains
`.env*` (excluding all `.env` variants) with an explicit `!.env-sample` negation, plus a header
comment explaining why secrets must never enter an image layer. `.git` is still deliberately kept
(the version-check feature in `core/version.py` needs it) - not a defect.

## RESOLVED 2026-08-24: a new pin never gets its parcel, because creation marks the lookup as already done

**Confirmed live on a clean database with production REData reachable**, not
inferred. This is the root cause of most of what looks broken about place data
for a newly pinned location: no parcel boundary, no shared-property resolution,
no building child pins, no floorplan footprint, and a wiki with no geometry.

**What happens.** `create_pin_for_profile` calls `resolve_location_place` when
`place_resolved_at` is NULL (`services/pins/pin_creation.py:256-257`). That
function is explicitly a cache read - its own docstring says "Never calls a
provider - it only asks what is already known" - but it stamps the timestamp
regardless of whether it found anything:

```python
# services/places/resolution.py:44-46
place = Place.objects.resolve_for_point(location.latitude, location.longitude)   # may be None
if save and (location.place_id != (place.pk if place else None) or location.place_resolved_at is None):
    Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=timezone.now())
```

Every trigger for the provider chain reads exactly that field as "the chain has
run":

```python
# services/locations/boundaries.py:208, in generation_status
if location.place_resolved_at is None:
    return False, False
```

So immediately after creation `generation_status` returns `(ran=True,
stale=False)`, and therefore:

- `schedule_location_boundary_generation` returns False - "already fresh"
- `BoundaryPanelSource.is_ready` returns True, so the lazy panel never fetches
- `enrich_wiki_location` skips `generate_location_boundaries`

Nothing is left to run it. `Location.save()` at `models/location/model.py:462-465`
deliberately leaves `place_resolved_at` unset *for this exact reason*, and
`pin_creation` then sets it.

**Measured**, on an empty database, pin at 41.733181, -73.928493 (Hudson River
State Hospital):

```
AFTER CREATE: place_id=None  place_resolved_at=2026-08-24 20:14:05+00:00
AFTER CREATE: generation_status ran=True stale=False
AFTER CREATE: schedule_location_boundary_generation -> False
```

The data is not the problem: production REData answers this coordinate with
parcel `c912a64b-7b07-45d4-9e51-89e80a25067f` and **33 buildings** from NY SHPO's
CRIS inventory, complete with building numbers. Forcing
`generate_location_boundaries(location)` by hand does produce geometry.

**How long the pin stays wrong.** `generation_status` also computes staleness
against `SiteSettings.boundary_cache_days`, which is **60**. So the chain becomes
eligible again 60 days after the pin was created - which is indistinguishable
from never for anyone looking at the pin they just made.

**Fix, and the thing to be careful about.** The stamp should mean "a provider ran
and this is what it found", which is what `BoundaryPanelSource.is_ready`'s own
comment assumes ("stamped even when nothing was found, so a fruitless run
doesn't retrigger the chain on every page view"). The narrow change is for
`resolve_location_place` not to stamp - it is a cache read, and the field is a
provider-run marker - leaving the stamp to `generate_location_boundaries`. The
care needed is that `resolve_location_place` is also called from
`resolve_locations_in` on every geometry change, so whatever replaces this must
not make a busy parcel re-run the chain per re-resolution.

Guarded by `tests/integration/specs/location/hrsh-boundary.spec.ts`, which is
written to fail on exactly this and to say so.

**Resolved.** `resolve_location_place` now stamps only when the resolved place
actually changed, so a coordinate it resolved nothing for is left unstamped and
`generate_location_boundaries` remains the only thing that records a genuine
provider miss. The `resolve_locations_in` concern above is handled by the same
condition: a re-resolution that does not move a location writes nothing at all,
so a busy parcel does not re-run the chain per re-resolution.

Two further defects were found while confirming this one, both fixed with it:

- **The same function wiped the place it had just resolved.** It wrote the
  timestamp to the database but not to the in-memory instance, and
  `generate_location_boundaries` reads that attribute immediately afterwards to
  decide whether to record a miss - so a freshly provisioned place could be
  cleared straight back to `None` by `attach_location(location, None)`. This is
  the likelier explanation for `place_id=None` alongside a stamped
  `place_resolved_at` on rows where the chain genuinely did run.

- **A boundary we invented outranked one REData gave us.** See the entry below;
  fixing the stamp alone still left the wrong shape on the map.

## RESOLVED 2026-08-24: the map drew a hull around our own child pins instead of REData's parcel

Reported from the e2e deployment: "That parcel boundary is absolutely
incorrect... it looks like a generated boundary created to ensure that all the
pins it thinks are inside the parcel are within the boundary."

That reading was right. REData answers **six** scored candidates for this parcel
and flags one `is_suggested` (240,740 m²); the app drew the convex hull of the
campus pin and its three child pins (measured 154,753 m² as rendered) - an
outline of *the markers we happen to know about* rather than evidence about the
property. It is a legitimate last resort and a bad thing to prefer: on screen it
is indistinguishable from a surveyed parcel, and it grows and shrinks with the
set of buildings that happen to have been imported.

`BoundaryManager.resolve_for_pin` returned the pin's own `generated_polygon`
second, ahead of the place, so geometry the chain had just fetched stayed
invisible on the very page that fetched it. A hull carrying
`generated_from_children` now yields whenever a provider outline exists; every
other generated row (the pre-places location default among them) keeps the
precedence it had, and a person's own drawing still outranks everything.
`refit_child_pin_boundary` drops a stand-in a provider has superseded rather
than refitting a shape nothing will draw.

**Not fixed here, and deliberately.** REData currently suggests
`ny_cris (Hudson River State Hospital, Main Building)` for these coordinates,
which the site owner considers the wrong choice - the subdivision boundary
`ny_cris (Hudson Heritage Development/Former Hudson River State Hospital/Property Subdivision)`
(1,163,489 m²) is the better answer, and that selection is REData's to correct.
UrbanLens' obligation is to draw what REData suggests rather than to invent an
alternative, which is what this fix establishes.

Guarded by `tests/integration/specs/location/hrsh-boundary-provenance.spec.ts`,
which asserts provenance rather than presence - the older boundary spec passes
on an invented hull, because one does arrive and is plausibly sized. Unit
coverage for the precedence rules is in
`dashboard/tests/hypothesis/test_redata_parcel_beats_generated_hull.py`.

## ~~OPEN 2026-08-24: concurrent requests to `schema/` can 500~~ - RESOLVED 2026-08-25

Two overlapping fetches of `/dashboard/api/external/v1/schema/` could produce a
500, with the next request answering 200 normally. The traceback was inside
drf-spectacular rather than app code: `_load_class` resolves an extension's
`target_class` from a dotted string to the class object, in place, on first
use, with no lock - two gevent-concurrent requests could race over that
mutation, one reading `target_class` after the other had replaced it (or while
it was `None`), producing `AttributeError: 'NoneType' object has no attribute
'startswith'`. Not caused by anything in this codebase (nothing here registers
a drf-spectacular extension) but newly visible once `tests/contract` and the
Playwright `api` project both started fetching the schema and could overlap;
almost certainly long-standing. It mattered because the schema is a deployed
artefact - a client generating code against it got an intermittent 500 with no
indication that retrying would work.

**Resolved 2026-08-25** (`5483e74e`). Since this is third-party code that can't
be durably edited in-repo, added `external_api.schema.patch_extension_thread_safety()`,
which replaces `OpenApiGeneratorExtension._load_class` with a version that
wraps the original (captured via the class's own `__dict__`, so drf-spectacular's
real logic is reused rather than duplicated) in a module-level `threading.Lock`,
double-checking `isinstance(cls.target_class, str)` after acquiring it so a
class already resolved by the lock-holder isn't reprocessed. Idempotent. Wired
into `DashboardConfig.ready()`, so it's installed before any request can be
served. Guarded by `test_schema_extension_thread_safety.py`, which proves the
wrapper holds the lock for the full duration of a (deliberately slowed)
resolution, so a second caller cannot acquire it mid-resolution.

## RESOLVED 2026-08-24: re-adding a removed friend creates a request nobody can accept

Remove a friend, change your mind, and send them a friend request again. Both
people can see the request. Neither can act on it, permanently.

**What is established.** `DELETE friends/{profile_uuid}/` soft-deletes: the
`Friendship` row survives with status `Removed`, keeping the `from_profile` it
originally had. When a `POST friends/` later meets such a row, the request can
end up recorded in the **old** direction - A sends to B, and B sees it as
`status="Requested" direction="outgoing"`, an incoming request labelled as one
they sent. `friends/{A}/accept/` then looks for an incoming request from A,
finds none, and answers `404 Friend request not found`. Both people can see the
request; neither can act on it.

**What is not established, and is the next thing to find out.** The obvious
mechanism - `Friendship.objects.all().between()` finding the soft-deleted row
and reviving it without re-orienting `from_profile` - is *not sufficient on its
own*. A test that constructs exactly that state (B befriends A, A accepts, A
removes, A re-requests) **passes**: that revival orients correctly. Only the
first test to run against a *previous run's* leftovers fails, so the row it
meets must be in some further sub-state this reproduction does not recreate -
removed-while-still-requested, or declined-then-removed, or similar. Whoever
picks this up should dump the surviving row's full record rather than trusting
the reconstruction above.

The feed shows it plainly once you ask for the right status - the requester sees
`direction="incoming"` for a request they sent:

    requester still sees status="Removed" direction="incoming" under ?status=Removed
    recipient still sees status="Removed" direction="outgoing" under ?status=Removed

**How it was found**, because the path is instructive. It surfaced as an
intermittent 404 in the integration suite that only ever hit the *first* test in
the file - the one meeting the previous run's leftovers. Three plausible
explanations were wrong: that the `message` field was implicated (isolating it
proved the opposite - the request *with* a note accepted, the one without
failed), that the two clients shared an account, and that `direction` was
computed from the row rather than the viewer. What settled it was making the
test's cleanup *prove* the relationship was gone across all eight statuses
instead of the three it had been checking, at which point the surviving
`Removed` row named itself.

**Resolved, and the "further sub-state" above turned out not to exist.** The
mechanism *was* simply `between()` reviving the row without re-orienting it. The
reason the reconstruction passed is that the reconstruction was the wrong way
round: in "B befriends A, A accepts, A removes, A re-requests", A is already the
row's `from_profile`, so there is nothing to re-orient and the case was never
going to fail. The failing shape needs the *other* party to re-request - A asks
B, B accepts, B removes, **B** asks A - and that fails reliably from a clean
slate, no leftovers required. Anyone reading the paragraph above should take it
as a warning about reconstructions that confirm what you expected: it was
symmetric-looking enough to seem equivalent, and being wrong about it sent the
investigation looking for a state that was not there.

**The fix** re-orients the row in `Friendship.request`, with two details that
were not obvious from the diagnosis:

- `unique_together` is `(from_profile, to_profile)`, so A->B and B->A can both
  exist (see "reciprocal `Friendship` rows" elsewhere in the live file).
  Swapping a row's ends can therefore collide with a real row, and the fix
  prefers an already-correctly-oriented row when one exists rather than
  swapping blind.
- `muted_by_from_profile` / `muted_by_to_profile` are **positional** - which
  column is yours depends on which end of the row you are. Swapping the ends
  without swapping these hands one person's mute to the other, silencing the
  wrong party invisibly. That is a worse bug than the one being fixed, and it
  has its own test.

The suggestion to ask the same question of every status `between()` can return
was half wrong, which is the other correction worth keeping: `can_request`
admits `Declined` and `Removed` only. For `Blocked` and `Ignored` the correct
behaviour is a **refusal**, and re-orienting one of those rows would have been a
new defect. Both now have tests asserting the refusal, sitting directly beside
the re-orientation.

Guarded by `test_friendship_revival_direction.py` (7 tests), proved non-vacuous
by reverting the fix - 3 of them fail. Originally observed by
`tests/integration/specs/api/social.spec.ts::a request is visible to the
recipient, and acceptance to both`, which failed only as the first test in that
file, i.e. the one that met the previous run's leftovers.

## RESOLVED 2026-08-24: a photo upload trusts the filename, not the bytes

`POST /dashboard/api/external/v1/photos/` accepts a shell script sent as
`not-really.png` with `Content-Type: image/png` and answers **201**. The file is
stored and served back from the photo's `url` as an image.

Both signals the endpoint appears to trust - the extension and the declared
content type - are supplied by the caller, so neither is evidence of anything.
The project already carries `filetype` as a dependency and has a malware-scan
service, so rejecting a file whose magic bytes are not an image looks like the
intent rather than a new feature.

**It hid behind duplicate detection.** The first version of the test reused the
same payload every run and saw a `409` on the second and later runs, which reads
as "refused" and is really the store recognising the file it accepted the first
time. The test now embeds the run id in the bytes so every run is a first
upload. Anything else asserting on upload rejection should do the same.

How much this matters is worth deciding rather than assuming: a browser will not
execute a shell script served as `image/png`, so this is not remote code
execution. What it is, is an unbounded arbitrary-file store attached to any
account with a `photos:write` key, whose contents are served from the
application's own origin.

Found by `tests/integration/specs/services/media-storage.spec.ts`, which stays
red until the bytes are checked.

**Fixed**: `image_upload_error` now requires a *positive* image identification
for `MediaKind.PHOTO` rather than letting sniffing fail open. Guarded by
`test_photo_bytes_must_be_an_image.py` (11 tests).

**The fix nearly caused a worse regression than the defect**, and this is the
transferable part. Failing closed is only safe if every allowed image extension
has a magic-byte signature the library recognises *under the same name* - and
two did not: `filetype` reports a TIFF as `tif` and an animated PNG as `apng`,
neither of which was in the photo allowlist. Shipping the strict check without
noticing would have started rejecting genuine TIFF and APNG uploads, trading a
security hole for a broken feature. It also broke eight existing tests that
uploaded `b"photo-bytes"`, which is now correctly refused; those were moved onto
real image bytes from a new `core/tests/images.py`. When you make a check
stricter, ask what it now rejects that it should not.

## RESOLVED 2026-08-24: a visit can be logged in the future

`POST /dashboard/api/external/v1/pins/{pin_slug}/visits/` accepts a
`visited_at` a week from now and answers **201**.

A visit is a record of somewhere the user has *been*. A future one is not a
mistake the API should store: it propagates into "last visited" everywhere that
is displayed and ordered by, so one fat-fingered year makes a pin permanently
the most recently visited thing the user owns. It also has no legitimate
meaning - a planned outing is a trip activity, which is a different model with
its own scheduling.

The fix is a validator on the serializer field. Bounding it at "not after now,
give or take clock skew" is enough; nothing needs to reason about how far in
the past is plausible.

Found by `tests/integration/specs/api/visits.spec.ts`, which also pins the
timestamp round-trip - the neighbouring risk on that field is a deployment
whose database or worker is set to a different timezone shifting a visit by
hours, so it is compared as an instant rather than as a string.

**Fixed in both layers**, which is the part worth noting: the serializer
validator alone would have left `create_manual_visit` accepting a future
timestamp from every other caller, so the bound lives on the shared service too
(`VisitInFutureError`, `MAX_VISIT_CLOCK_SKEW = 5 minutes`) with the serializer
rejecting early for a clean 400. Guarded by `test_visit_time_bounds.py` across
the serializer, the service, and the endpoint.

## ~~`SavedFilterDetailView.patch` has a pre-existing mypy type error (found 2026-08-22, not fixed)~~ - RESOLVED 2026-08-25

`external_api/views.py`, `SavedFilterDetailView.patch`: `saved_filter.color = clean_color(data["color"], default="...")` -
mypy reported `Incompatible types in assignment (expression has type "str | None", variable has type
"str | int | Combinable")`. Found incidentally while mypy-checking an unrelated `PinList` fix in the
same file; not investigated at the time.

**Resolved 2026-08-25** (`5483e74e`'s sibling fix in `services/core/colors.py` - see the "Five mypy
errors outside the floorplan work" entry below, same root cause). `clean_color` now carries two
`@overload`s: `clean_color(value, *, default: str, ...) -> str` and `clean_color(value, *, default:
None = None, ...) -> str | None` - a `str` default now provably never returns `None`. Both call
sites passing `default=""` (a `str`) now type-check as plain `str` assignments. Fresh `mypy
--no-incremental` on `external_api/views.py` confirms zero errors.

## Five mypy errors outside the floorplan work (2026-08-23) - RESOLVED 2026-08-25

`mypy src/urbanlens` reported five errors in four files, none in code a floorplan pass had
touched. Left alone deliberately at the time (one file had uncommitted changes from other work in
flight; the rest were unrelated to that pass):

- `conftest.py:50` - an unused `type: ignore` *and*, on the same line, a real `setdefault` argument
  mismatch.
- `services/core/text_limits.py:52` - a `"type"` attribute error.
- `plugins/builtin/property_records.py:201`.
- `external_api/views.py:2119` - `clean_color(...)` returning `str | None` into a field typed
  `str | int | Combinable` (the same defect as the `SavedFilterDetailView.patch` entry above).

**Resolved 2026-08-25.** Fresh, cache-cleared `mypy --no-incremental src/urbanlens` (`rm -rf
.mypy_cache` first) reports "Success: no issues found in 861 source files", confirmed twice.
Root-cause fixes are in place for all five, each with its own comment: `conftest.py:55` narrows
`sys.modules.setdefault("hypothesis.extra._patching", None)  # type: ignore[arg-type]`, noting the
previous ignore was miscoded and silenced the wrong error; `text_limits.py`'s
`column_max_length` (43-67) uses `getattr(field, "max_length", None)` + `isinstance(max_length,
int)` narrowing instead of assuming every `_meta.get_field()` result has `.max_length`;
`property_records.py:201`'s `_tax_status` builds `entries = [row for row in rows if
isinstance(row, dict)]` first; `external_api/views.py:2119` is fixed by the `clean_color` overloads
described above. `warn_unused_ignores=true` is on and not disabled, so the zero-error run also
proves the `conftest.py` ignore is not dead - it is actively suppressing the real error its comment
names.
