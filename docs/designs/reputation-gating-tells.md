# What gives a gated wiki away

Companion to `reputation-and-gating.md`. Produced 2026-08-24 by a six-channel audit of this
branch against one question:

> A gated viewer sees a vulnerable wiki with community content withheld. What can still tell
> them apart from a place nobody has documented?

82 findings survived adversarial verification. They are reproduced below as the synthesis
grouped them — by root cause and structural class, not by channel — because the class-level
fixes are worth far more than the point fixes.

**The headline: filtering community content out of the existing wiki does not work.** The
empty state is not a filtered view of a community wiki; it is *a row the viewer owns*. See §4.

# Anti‑probing gate: consolidated engineering findings

## 0. Executive verdict

The 82 channel findings collapse to **eleven structural classes and three root causes**. The three roots are:

1. **`Wiki.objects.get_for_location()` / `existing_for_location()` take a `Location` and no viewer** (`models/wiki/queryset.py:91-129`). Every surface in the app asks this one function "does this place have a community wiki?" and is told the truth. There is no seam for a viewer-dependent answer.
2. **`Pin.wiki` is a stored FK, written at pin creation** (`services/pins/pin_creation.py:287`) and read as an access shortcut (`models/boundary/queryset.py:324`, `services/pins/pin_detail.py:92`). It is a denormalised copy of the answer to (1), so fixing (1) alone leaves the leak stored on the attacker's own row.
3. **`visible_wiki_location_ids()` / `_cached()` (`services/wiki/wiki_access.py:250-254`) is the app's only notion of "wikis in reach", and it is the pin‑discovery rule verbatim** — the exact predicate the attack model grants the attacker for free. Search, autocomplete, device scans, photo containment, games and notifications all consume it.

Fix those three and roughly 55 of the 82 findings close at once. The remainder are genuinely independent and are listed in classes E, H, I, J and K below.

---

## 1. The empty‑state baseline (what every fix must target)

This is the target, verified against code. It has **two phases**, and the second is the one that matters, because the spec requires the Create button to keep working.

### Phase 1 — an undocumented place the viewer has pinned, before they click Create

* A background **draft** `Wiki` row exists (`tasks.py:27-61`, `models/wiki/queryset.py:161-182`), named `location.official_name` or `"Unnamed Location in <area>"`, later possibly overwritten by `PlaceNameResolverChain` (`tasks.py:100-124`). It is invisible to `get_for_location` (`queryset.py:115-129`).
* Pin hero renders `pin-hero-wiki-box--create` (`partials/pins/_pin_detail_hero_body.html:93`), **not** the `<aside class="pin-hero-wiki-box">` at line 58. No detach button, no swap button, no `location.display_name` link.
* Child‑pins panel: `has_wiki: False` → no pull button; "No child pins yet".
* Boundary: `source ∈ {place, generated, circle}` or `polygon: null`; **never `"wiki"`**. `pending: true` on the first‑ever view of that Location.
* External API: `wiki_slug: null`, `wiki_exists: false`, `created: 0`.
* `GET /wikis/<location_slug>/` → **404**.
* Safety `wiki-option` fragment renders **nothing**.
* Global search / autocomplete return no wiki, article, comment or wiki‑photo rows (drafts are excluded by `officially_created` in search — though **not** in `services/map_pins/autocomplete.py:138-162`, which is a live draft‑visibility bug independent of this feature).

### Phase 2 — immediately after the viewer clicks "Create Community Wiki" on a virgin place

`claim_for_location` (`queryset.py:207-225`) creates or promotes the draft with `officially_created=True, created_by=<viewer>`, and `WikiCreationService.create_for_pin` (`services/wiki/wiki_creation.py:88-137`) seeds **only the viewer's own** chosen fields, aliases, photos and stat votes. The resulting page is:

* **About card**: only what they seeded; suggest‑edits dialog prefilled with the same; all eight security selects `unknown` unless seeded.
* **Stats**: `composite.rounded`/`exact` equal *their own* vote; `count` displays 0 / "No votes yet", because 1 < `MIN_VISIBLE_PIN_COUNT = 3` (`services/wiki/community_counts.py:28,51`). A composite that disagrees with `my_vote` is proof of hidden voters.
* **Community card**: `pin_count_low: True`, `pin_count_approx: None`, `first_pinned: None` (`community_counts.py:103-117`).
* **Comments tab**: no badge (`wiki.comments.count() == 0`).
* **Changes tab**: "No edits have been recorded yet." — creation writes **zero** `WikiEdit` rows (confirmed: no `WikiEdit` writer in `wiki_creation.py`).
* **Delete button renders** (`can_delete_wiki` = created_by self ∧ ¬viewed_by_other, `models/wiki/model.py:316`).
* **Aliases panel**: possibly non‑empty — `persist_official_aliases_for_location` backfills geocoder aliases with `created_by=NULL` (`services/locations/naming.py:688-715`), so the discriminator is the **author chip**, not the count.
* **Detail pins**: possibly non‑empty — `_seed_building_wikis` mirrors confident REData buildings into child wikis when the parcel has ≥ `MULTI_BUILDING_THRESHOLD` (`wiki_creation.py:119-130`). Do not assume `[]`.
* markup JSON, custom‑layers JSON, overlays JSON, photo‑map JSON: `[]`. Cover photo: only if they seeded one.
* Article: none → `base_revision_id` null; first save returns **200 "Article saved."**
* `HX-Trigger: {"wikiCreated": {"created": true}}`; `wikis_created` achievement **+1**.

**The operative observation:** phase 2 is not a filtered view of a community wiki. It is a wiki row the viewer owns, with their attribution, their votes, their edit history, and a delete button. No amount of response filtering over someone else's row reproduces it. That fact drives the recommendation in §4.

---

## 2. Ranked kill list

**Tier 0 — one page load of the attacker's own pin page, zero interaction.** Any one of these alone defeats the feature.

1. **Hero renders the wiki box instead of the create button** — `_pin_detail_hero_body.html:57`, driven by `Pin.community_wiki` (`models/pin/model.py:738-753`). *(merges #1, #52, #71)*
2. **Boundary payload labels the source `"wiki"`** — `models/boundary/queryset.py:324-326`, surfaced at `controllers/boundary.py:93-94`, `services/pins/external_data.py:854`, and unlabelled‑but‑community geometry at `pin_detail.py:95`. `"wiki"` is unreachable on a virgin place; the only writers of wiki‑owned Boundary rows are community edit paths. *(merges #6, #44, #57, #68)*
3. **`Location.display_name` returns the community name with no viewer** — `models/location/model.py:296-317` → `Pin.effective_name` (`pin/model.py:756-758`) → map payload (`services/map_pins/payload.py:101`), pin cache, sync, `to_json`. *(merges #10, #37, #69)*
4. **Child‑pins panel `has_wiki` flag** — `controllers/detail_pins.py:131` + `detail_pins_panel.html:6-12`. *(#11)*
5. **`wiki_slug` in the pin‑detail payload** — `services/pins/pin_detail.py:92`, three external endpoints. *(merges #5, #34)*
6. **Safety check‑in wiki‑notify fragment** — `controllers/safety.py:1227-1229` + `_wiki_notify_toggle.html:15`: name, link, `last edited <naturaltime>`, `could notify N people`, keyed on arbitrary query‑string coordinates. Strictly worse than existence: it reports **contributor count and recency**. *(merges #14, #66, #70)*
7. **Warm shared infrastructure** — pre‑opened panel tab (`controllers/pin.py:268`), coordinate‑keyed slides `ready_key` (`external_data.py:986`), boundary `pending:false` (`controllers/boundary.py:150-155`), locally materialised media copies served from your own host (`services/media/media_relevance.py:361-366`). *(merges #73, #74, #75, #76)*

**Tier 1 — one extra GET, no state change, fully scriptable.**

8. **`wiki_exists` boolean** in `external_api/views_pin_sync.py:74,112,158`. *(merges #4, #33)*
9. **`resolve_visible_wiki` answers 200 vs 404** — `services/wiki/wiki_access.py:388-393`, the single resolution point for all 25 wiki view classes. There is no fourth branch for "exists but must read as absent". *(#36)*
10. **Global search: wiki / article / comment / photo providers** — `services/global_search/providers.py:478, 525, 867, 383`, scoped only by `visible_wiki_location_ids_cached`. Returns `excerpt(wiki.description, …)` — the withheld text itself. *(merges #7, #39, #78)*
11. **Map autocomplete** — `services/map_pins/autocomplete.py:96-98` (own‑pin OR clauses on hidden `name`/`aliases`/`description`: a blind substring oracle) and `:138-162` (community block, no gate, no `officially_created`, returns wiki name + exact lat/lng). *(merges #8, #9, #38, #77)*
12. **Pin‑move 409 names the wiki** — `external_api/views.py:823`, `services/wiki/wiki_access.py:283-330`. *(#35)*
13. **Device‑scan nearby markers** — `external_api/views_device_scans.py:105`. *(#41)*
14. **Games as content channels** — consensus eligibility `no_eligible_wikis` and round payloads (`services/consensus/eligibility.py:58-80`, `serializers.py:50-52`); trivia questions **AI‑mined verbatim from `wiki.description`** and served on pin‑only eligibility (`services/trivia/generation.py:85,111-117`, `eligibility.py:59-68`). *(merges #61, #62, #82)*
15. **Search hint chips** — `controllers/search.py:59-73`; bare `"wikis"` verifies as "this profile can see ≥1 official wiki". *(#79)*

**Tier 2 — one write probe, reversible, leaves little trace.**

16. **`POST /wiki/create/` returns the existing wiki, its hero, and `created:false`** — `controllers/wiki_create.py:51,90,92`; and `wiki_creation.py:102` permanently stamps `pin.wiki`. *(merges #2, #52, #71)*
17. **Any write echoes the community projection back** — field edit returns `about_html` rendered from the raw `wiki` (`controllers/location_wiki.py:361-373`) plus a per‑field equality oracle at `services/wiki/wiki_edits.py:127`; stat vote returns the full composite (`location_wiki.py:557`); alias add returns the whole hidden alias list (`controllers/aliases.py:240`); link add returns the whole link list (`controllers/links.py:74`); owner add merges into and re‑renders hidden owner rows (`controllers/property_owner.py:371-375, 333`). *(merges #46, #49, #50, #51, #56, #17, #81, #43)*
18. **Uniqueness collisions** — alias 409 "That alias already exists." (`aliases.py:233`, constraint `models/aliases/model.py:157`), link 400 (`links.py:129`), article 409 on first save because `Article.wiki` is OneToOne (`services/wiki/articles.py:405-407`), child‑wiki coordinate 400 (`detail_pins.py:114`), layer name collision toast (`custom_layers.py:378`). *(merges #47, #50, #51, #54, #55, #15)*
19. **Pin↔wiki sync exfiltrates the payload** — `pull_children_from_wiki` copies every community child wiki's name, type and exact coordinates into the attacker's own pins (`services/pins/pin_wiki_sync.py:213-249`), authorised on pin ownership alone (`controllers/pin_wiki_sync.py:69`); the push side dedupes against hidden markers and reports the count (`pin_wiki_sync.py:166-173`), with a three‑way toast split at `:34-38`. *(merges #3, #48, #53)*
20. **`WikiEdit.changes` stores the community's prior value inside the viewer's own edit row** — `services/wiki/wiki_edits.py:160`, rendered at `wiki_history.html:22`. Type one character into the "empty" description, read 80 chars of the hidden one from your own history, revert. Same pattern for boundary WKT (`controllers/boundary.py:256,294`) and alias/link removals. **This survives a perfect read gate**, because it lands in content the spec promises always to show. *(#58)*
21. **Delete‑wiki 403 states in English that other people have viewed the page** — `controllers/location_wiki.py:317`, `models/wiki/model.py:316`. The inverse (button absent) is `_wiki_detail_hero_body.html:32-41`. *(merges #18, #59)*

**Tier 3 — aggregates and counters; need a comparison or a wait, but are unfixable by content filtering.**

22. **Community card publishes pinner count and first‑pinned month** — `services/wiki/community_counts.py:102-117`. Pin‑table derived; no wiki‑content filter touches it. Fuzz is ±2 floored at 3, so it never collapses back into "fewer than 3". *(merges #16, #43)*
23. **Comments tab badge** — `controllers/location_wiki.py:189`, `wiki.html:37`, rendered in `{% block subnav %}` before the lazily‑loaded list any filter would apply to. *(#20)*
24. **`wikis_created` achievement fails to increment** — `services/achievements/metrics.py:251-255`; `claim_for_location:219-220` leaves `created_by` untouched. Readable only while the tier is unearned — i.e. exactly the low‑reputation account the gate targets. *(merges #63, #72)*
25. **Pin‑list sort order derived from the hidden name** — `controllers/maps.py:556`, `Lower(Coalesce("name", "location__wiki__name", "location__official_name"))`. Binary‑searchable with ruler pins. *(#80)*
26. **Media tiles carry unfuzzed community vote scores and re‑sort by them** — `models/images/queryset.py:320-340`, `pin_media_items.html:22,59`. Place‑derived, so a wiki‑content gate misses it. *(#25)*
27. **Enrichment queue ordering** is `profile_count*3 + list_count*2 + …` (`services/locations/enrichment.py:471-479`) and its output (Street View / satellite imagery) is user‑visible. Timing correlation only. *(#67)*
28. **Draft asymmetry** — `get_or_create_draft_for_location` short‑circuits when any wiki already covers the place (`queryset.py:178`), so a gated viewer's Location gets **no draft and no enrichment run at all**. Data‑model deep. *(#45)*

**Cross‑account channels.** Safety check‑in escalation notifies every profile with a pin at the location, naming the wiki, over SMS/WhatsApp/WebSocket, and is attacker‑triggerable via `notify_community_wiki` (`services/visits/safety.py:1677-1746`, `:1986-2005`) *(#60)*. Comment reply/reaction notifications deep‑link a wiki the recipient is gated on (`services/notifications/comment_notifications.py:146-190`, `:77-78`) *(#65)*.

Findings #13 (competing‑location picker), #64 (SpotGuessr named place), #42 (photo payload `wiki_name`), #40 (own‑row search cross‑match), #27 (discarded creation seeds) are real but downstream of, or subsumed by, the above; fix them with their class.

---

## 3. Structural classes and their class‑level fixes

**A. Viewer‑blind existence resolution.** *(items 1, 4, 5, 8, 9, 16, and most of 19)*
**Fix:** change the signature. `Wiki.objects.get_for_location(location, viewer=profile)` must return `None` when gated, and `resolve_visible_wiki` must raise a **third `Http404`** (`wiki_access.py:388`) so gated == undocumented == nonexistent at the transport layer. Body‑blanking inside `build_wiki_detail` is the wrong layer — the status line answers first. Then add a structural CI check (this repo already has three) that fails on any `get_for_location(` call without a viewer argument, mirroring how `.official()` was introduced after the draft rule was enforced "one call site at a time and several missed it" (`queryset.py:33-41`).

**B. Denormalised `Pin.wiki` used as an access decision.** *(items 2, 5, 16)*
**Fix:** ban `pin.wiki_id` as a shortcut in `resolve_for_pin` and anywhere else; give `Boundary.objects.resolve_for_pin` a viewer argument (it currently has none, which is why every caller leaks). Stop `create_for_pin` stamping the FK on an already‑official wiki. Drop `wiki_slug` from the pin payload entirely — the serializer documents it as informational and never accepted by any wiki route.

**C. Reach computed from the pin‑discovery rule alone.** *(items 10, 11, 13, 15, 26, plus #42, #64)*
**Fix:** subtract gated locations inside `visible_wiki_location_ids` / `_cached`. One edit fixes four search providers, device scans, the image `visible_to` container clause (`models/images/queryset.py:81`) and the custom‑field picker. Separately, delete the `location__wiki__name|aliases__name|description` OR clauses from every own‑row query (`autocomplete.py:96-98`, `global_search/providers.py:325-326,406,628`, `models/pin/queryset.py:321`) — they buy nothing the viewer's own text doesn't already give, and they are the substring‑extraction channel.

**D. Affordances rendered iff content exists — including inverted ones.** *(items 1, 4, 21, plus #13, #23)*
**Fix:** every affordance must be driven by the gated resolver, and the *inverted* cases must be reproduced too: the delete button must render, the boundary‑vote dialog must auto‑open (`services/geo/boundary_voting.py:289`), the "wikis" search‑hint chip must verify. A gate that only hides things is detectable by what fails to appear.

**E. Aggregates computed before, or without, filtering.** *(items 17, 22, 23, 26, and #43)*
**Fix:** every aggregate takes a viewer and is computed over the visible set. `WikiStatVoteQuerySet.composite` takes a `Wiki`, not a queryset — that API must change, because `rounded`/`exact` bypass the fuzz entirely (`models/wiki_stat_vote/queryset.py:78,95`) and the vulnerability rating is one of these fields. **Fuzzing a count is not gating it**: `approximate_pin_count` floors at 3, so it can never impersonate "fewer than 3".

**F. Write‑path collisions and echoes.** *(items 17, 18)*
**Fix, two parts.** (i) Every write response is re‑rendered from the same viewer‑filtered projection the read path uses, and the no‑op / collision branch is made byte‑identical to the applied / created branch. (ii) Uniqueness collisions cannot be papered over — `db_walias_unique`, `db_wlink_wiki_url_unique` and `Article.wiki` OneToOne are per‑wiki constraints. The only correct answer is a viewer‑scoped write target (§4).

**G. Audit trails that persist unshown values.** *(item 20)*
**Fix:** never write a `from` value the editor was not shown. Store a redacted marker or a server‑side pointer, and keep `revert_wiki_edit` working off the pointer.

**H. Bulk payloads inlined into the page body before any tab filter runs.** *(#19, #24, #28, #29, #30, #31, #32, #22)*
**Fix:** these are the withheld content, not side channels — they close only via class A's 404. Do not attempt per‑panel filtering; that is precisely the "one call site at a time" failure mode. Note `wiki.cover_photo.image.url` (`wiki.html:57`) never passes through `Image.objects.visible_to`, which is a live bug independent of the gate.

**I. Shared infrastructure warmth and timing.** *(item 7, 27, 28)*
**Fix:** readiness must be reported per viewer. `SlidesPanelSource.ready_key` is keyed on coordinates alone; `panel_readiness` reads `LocationCache` by location id; `boundary_generation_ran` reads a persistent column. For a gated viewer, force the cold first‑view sequence (`default_panel_tab_key = None`, `pending: true`, provider URLs not local copies) regardless of shared warmth. Drop `profile_count`/`list_count` from `prioritized_location_candidates`.

**J. Third‑party and cross‑account channels.** *(safety escalation, comment notifications, games)*
**Fix:** any feature selecting over "wikis in reach" consumes the gated reach set (class C); recipient sets in `post_checkin_to_community_wiki` (`safety.py:1713`) are gate‑filtered; notification titles/SMS bodies drop `wiki.name`.

**K. Viewer‑less name resolution.** *(item 3, 25)*
**Fix:** `Location.display_name` must take a viewer, or be replaced at every call site by a clearance‑aware helper. Given its ~8 consumers (payload, sync, search subtitles, comments @mentions, SpotGuessr, pin list ordering, the pin cache), a viewer‑less property returning community text is the underlying design defect. Sort order must use the same clearance‑aware expression the display uses.

---

## 4. Honest assessment: is this workable on this codebase?

**As specified — "filter community content out of the wiki surfaces" — no.** Three independent reasons, each sufficient:

1. **The empty state is not a filtered view; it is a different row.** §1 phase 2 shows the genuine first‑arrival owns the wiki: `created_by` is them, the delete button renders, their votes *are* the composite, their edit is the entire history, `wikis_created` increments, `created:true` comes back. Reproducing that by filtering someone else's row means faking attribution, faking a composite, faking a delete affordance that must then *appear* to succeed (item 21), and faking an achievement delta. Every one of those is a lie the write path will eventually contradict.
2. **Uniqueness constraints and the OneToOne `Article` make the write path structurally undeniable.** `Article.wiki` is a OneToOne (`models/article/model.py:65-71`), so the gated viewer's "blank canvas" *is* the community's row; `latest_revision_id` conflicts on first save 100% of the time (`articles.py:405-407`). Alias and link uniqueness are per‑wiki. You cannot make a collision look like a creation while also making the created thing persist and later merge.
3. **Shared warm state and background‑work asymmetry are outside the request path** (class I, item 28). The spec says "EXACTLY what they would see if no other user had ever contributed". Other users warmed `LocationCache`, set the coordinate‑keyed slides ready‑marker, ran boundary generation, and *suppressed the gated account's own draft and enrichment run*. Simulating a cold first arrival costs either real duplicate API calls or deliberate fake latency, and the enrichment asymmetry is a data‑model decision, not a serializer one.

**What is workable: a copy‑on‑write shadow wiki.** Route a gated viewer's *entire* wiki interaction — read and write — to a private `Wiki` row on their own `Location`, created and enriched exactly as a first arrival's would be (which also fixes item 28: give the gated account its own draft even when a community wiki covers the place). Their creates, edits, aliases, links, article, votes, photos and child pins land there; every aggregate is computed over that row; every affordance is genuine; the delete button really deletes their row. `get_for_location(location, viewer)` returns the shadow when gated and the community row otherwise, and `resolve_visible_wiki` needs no special case at all. When reputation crosses the threshold, merge shadow into community.

That reframes the work from "audit 60 surfaces forever" to "one resolver, one reach set, one merge job" — and it is the only shape under which classes A, D, E, F and G close by construction rather than by vigilance.

**Two residual risks you should accept explicitly, because no design removes them.**

* **Un‑gating is itself an oracle.** The moment reputation crosses the threshold the page fills in. Attacker plan: probe 200 addresses cheaply, earn reputation, diff. Mitigate by making the threshold gradual and per‑place rather than a cliff, and by never back‑filling notifications for the newly visible period — but the diff exists.
* **The attacker can rent an ungated account.** The whole feature is a reputation tax on probing, not a security boundary. Scope your success criteria to "raises the cost of bulk automated probing", and say so in the design doc, because "COMPLETELY UNAWARE" is not achievable against a patient attacker with two accounts.

**One more thing worth flagging before implementation:** six independent channels found 82 tells and the last two channels were still finding *new* fatal ones (#14, #28, #29, #30, #31, #32, #44, #58). The tail is not exhausted. Any plan that relies on enumerating surfaces is already known to fail on this codebase — the `officially_created` draft rule was enforced call‑site by call‑site, several sites missed it, and `.official()` was added afterwards to stop the bleeding (`queryset.py:33-41`). Commit 5357d400 fixed five of those five days ago and #8, #38, #64 and #77 show at least three more still open. Build the gate as a property of resolution and reach, with CI enforcement, or it will leak the same way.