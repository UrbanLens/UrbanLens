# Concealed wiki — implementation specification

Produced 2026-08-24 by a six-surface classification pass over this branch, against R16 in
`reputation-and-gating.md` (Jess's rules: hide user-contributed content, show automatically
fetched content, always unset security indicators, always hide markup).

310 pieces of information were classified. The shape of the answer is the first useful
finding:

| Classification | Count |
|---|---|
| DERIVED (counts, orderings, timestamps, booleans over other rows) | 135 |
| USER (conceal) | 70 |
| MIXED (can hold either; provenance often unrecorded) | 61 |
| ALWAYS_HIDE (security indicators, markup) | 22 |
| AUTOMATIC (show) | 22 |

**Most of this work is not hiding fields.** It is recomputing aggregates over the visible set,
and the MIXED set is where the schema questions live.

One item in §0 is already settled and the spec predates the ruling: **§0.2 is answered — a
concealed wiki renders.** See R16's "unclaimed-place baseline" in the design doc. The create
affordance is a race-window artefact, so concealment must not route through
`get_for_location` returning None.

# Concealed Wiki — Implementation Specification

**Target:** `@release/v_0_7_0`. Companion to `docs/PRIVACY_MODEL.md` (which governs *who may reach* a wiki). This document governs *what a wiki shows once reached*. The two are conjunctive and independent: concealment never grants access, and access never disables concealment.

**The invariant, restated as a test:** for a concealed viewer, the wiki page, every HTMX partial it can pull, every JSON endpoint it can hit, and every external-API route that resolves through this wiki must be byte-equivalent to the same place with zero user contributions and the same enrichment history. Not "similar". Not "emptier". Equivalent.

---

## 0. Two things this spec does not decide

Flagged up front because implementation stalls on them.

**0.1 — The predicate.** Nothing in the codebase currently defines "concealed viewer"; `grep -rn "conceal" src/urbanlens` returns nothing. Every rule below is written against a single function `concealment_active(wiki, location, viewer) -> bool` that does not yet exist. Until the product owner defines it, implement it as a stub returning `False` and land the whole mechanism behind it. Do not scatter the predicate — see §4.

**0.2 — `officially_created` vs. the 404.** `resolve_visible_wiki` (`services/wiki/wiki_access.py:356-393`) 404s when `Wiki.objects.get_for_location` returns a draft, and its docstring says the 404 is deliberately indistinguishable from "no such slug". That directly contradicts the brief: a concealed wiki *must render*, not 404, because a 404 where the place plainly exists is the loudest possible tell. Either the concealed path bypasses the `officially_created` filter and renders the draft's automatic content as a brand-new page, or the flag stops gating page existence entirely. This needs a ruling before anything else is built; every other rule assumes the page renders.

Two smaller decisions in the same class:
- A wiki whose `PublicPinCandidate` reached `PASSED` is suggested by name to every opted-in profile site-wide (`services/pins/public_pins.py:342-381`). Concealment and public-pin status are mutually exclusive states.
- `public_vote.my_vote` is the viewer's own content, but it is only reachable through a block whose *existence* proves heavy community contribution (`public_pins.py:391`). This is the one place the own-content rule must yield.

---

## 1. The concealed-wiki specification

Organised by implementation layer. A concealed viewer sees exactly this.

### 1.1 `Wiki` model fields — the substitution table

Implemented as a read-only presentation proxy (§4), never by mutating rows.

| Field | File | Concealed value |
|---|---|---|
| `name` | `models/wiki/model.py:68` | `location.official_name` if set, else `WikiManager._placeholder_name(location)` (`wiki/queryset.py (draft/claim code, removed 2026-08-25)-133`). **Never** the stored value. |
| `slug` | `model.py:65` | Omit `wiki_slug` from payloads, or emit `slugify(concealed_name)`. The stored slug is a frozen snapshot of the name at first save and is never regenerated (`abstract/model.py:194-198`). |
| `description` | `model.py:69` | `""`. No automatic writer exists — every write path is a person. |
| `date_abandoned`, `date_last_active` | `model.py:71-72` | `None`. Sole writer is `wiki_edits.py:136-147`. |
| `effective_date_last_active` | `model.py:374-381` | `None` — falls out once both inputs are concealed. Do **not** conceal only `date_last_active`; this property re-exposes `date_abandoned` offset by one day. |
| `fences`, `alarms`, `cameras`, `security`, `signs`, `vps`, `plywood`, `locked` | `abstract/security.py:25-32` | `SecurityLevel.UNKNOWN`, all eight, unconditionally (rule 3). |
| `pin_type` | `model.py:74` | `places/scope.pin_type_for_place(wiki.location.place)`, else the `PinType.LOCATION_MARKER` default. |
| `pin_type_is_user_provided` | `model.py:78` | `False`. Leaving it `True` defeats the above — `effective_pin_type` short-circuits on it (`places/scope.py:156-157`). |
| `indoor_outdoor` | `model.py:86-92` | `None` (field default). |
| `color`, `icon` | `model.py:96-97` | `None`. |
| `detail_bg_color` / `detail_bg_opacity` / `detail_border_color` / `detail_border_opacity` | `model.py:101-104` | `None` / `80` / `None` / `100` — the *defaults*, not nulls. |
| `labels` (M2M) | `model.py:107-111` | Empty. Blanket rule; see §2.5. `categories` / `tags` / `statuses` (`abstract/labelled.py:57-70`) follow. |
| `location`, `place` | `model.py:115-131` | **Unchanged.** Provider data, rule 2. |
| `parent_wiki` | `model.py:136-142` | `None` (render no up-link). |
| `child_wikis` | reverse | Empty, everywhere (§1.2). |
| `created_by` | `model.py:146-152` | `None`. Matches a background draft (`wiki/queryset.py (draft/claim code, removed 2026-08-25)`). |
| `officially_created` | `model.py:163` | Not exposed; nothing user-visible may branch on it under concealment. |
| `viewed_by_other` | `model.py:167` | `False` on read — **and no write** (§1.6). |
| `cover_photo` | `model.py:173-179` | `None`. A brand-new wiki has no cover; do not substitute an enrichment photo. |
| `uuid` | `abstract/model.py:75` | Unchanged. |
| `created` | `abstract/model.py:46` | Month-truncated, or omitted. It is, to within a Celery hop, the moment the first user pinned the place (`tasks.py:27-61` queued from `models/pin/signals.py:360-362`). Must agree with the concealed `first_pinned` so the two cannot be differenced. |
| `updated` | `abstract/model.py:47` | `= concealed created`. Blanket rule; see §2.6. |
| Address proxies (`address`, `city`, `county`, `state`, `country`, `latitude`, `longitude`, `point`, `official_name`, `place_name`, `cid`, …) | `abstract/addressable.py:30-102` | **Unchanged.** Read-only delegations to `Location`; the wiki edit surface has no path to any of them. |
| `effective_latitude` / `effective_longitude` | `model.py:358-372` | Unchanged for the root wiki. Not emitted for child wikis (which are concealed wholesale). |

**Model methods:**

- `to_json()` (`model.py:410-426`) — substitute `name`, set `description=None`. The other nine keys are Location-sourced; leave them. Template-layer concealment is bypassed by this method.
- `to_detail_json()` (`model.py:428-443`) — never emitted. The child-marker payload is `[]`, not a list of stripped markers.
- `get_unique_search_name()` (`model.py:383-401`) — build from `official_name` + address components only; never fall through to `Wiki.name`. This shapes *outbound* provider API traffic.
- `__str__` (`model.py:407`) and `undo/handlers/wiki.py:84-85` — fall back to the automatic name, or `f"Wiki({self.pk})"`.

### 1.2 Wiki-scoped relation querysets — row-level filters

Each of these already has a `for_wiki()` / `for_location()` chokepoint. Concealment is an additional `AND`, applied there (§4).

| Relation | Chokepoint | Concealed set |
|---|---|---|
| `PinMarkup` | `models/markup/queryset.py:42` | `[]` (rule 4). Every wiki markup row has a non-null `profile` and only `MarkupCreateView` writes them. |
| `CustomLayer` | `models/markup/queryset.py:96` | `[]`. Also removes the per-layer toggle buttons appended at `map_components.py:356`. |
| `MapImageOverlay` | `models/map_overlay/queryset.py:17` | `[]` — **including** REData historical-map tile overlays. The imagery is provider data; the *row* is a record of someone aligning it. |
| `Comment` | `models/comments/queryset.py:22` | Viewer's own only (normally empty). Includes the safety check-in auto-comment (`services/visits/safety.py:1705`) — machine-composed but user-attributed; no exemption. |
| `WikiEdit` | `models/wiki_edit/queryset.py:13` | Viewer's own only. There is **no** system WikiEdit path — verified across all 13 writers. |
| `Image` (wiki gallery) | `models/images/queryset.py` + `wiki=` filters | `profile__isnull=True` only. See §2.12 — this is currently a null set. |
| `Album` / `AlbumItem` | `models/album/queryset.py:29` | `[]`. `Album.profile` is non-nullable; nothing auto-creates wiki albums. |
| `WikiAlias` | `wiki.aliases` | `source not in {AliasSource.USER, "wiki_sync"}`, and force `kind = OFFICIAL`/`ALTERNATE`. |
| `WikiLink` | `wiki.links` | Provider-discovered set only, re-derived from enrichment sources (§2.9) — not filtered from stored rows. |
| `Article` / `ArticleRevision` | `services/wiki/articles.get_article` | The newest revision with `editor_id IS NULL AND edit_summary IN SYSTEM_EDIT_SUMMARIES` (`models/article/model.py:36-37`), and only that revision. No system revision ⇒ no article. |
| `WikiStatVote` | `models/wiki_stat_vote/queryset.py:46` | Viewer's own only. |
| `Boundary` (wiki-keyed) | `models/boundary/queryset.py:125` | Excluded from resolution, so the chain falls through to `place` → `circle`. |
| `BoundaryVote` | `services/geo/boundary_voting.py` | Viewer's own only. |
| `WikiOwner` / `WikiPropertySale` | `models/property_owner/queryset.py:36,74` | `source == OwnerSource.OFFICIAL` only. |
| `Fact` / `FactEvidence` | `models/facts/queryset.py:25` | `[]`. Not currently rendered anywhere on the wiki — pre-emptive. |
| `WikiDeviceMarker` | `models/device_scan/queryset.py` | `[]`. A marker exists only because someone walked the site scanning. |
| `Floorplan` (community) | `services/floorplans/resolution.py:55` | Skip the `_community_plan` branch entirely; fall through to `_redata_document`. |
| `Wiki.child_wikis` | `controllers/detail_pins.py:320` | `[]` (blanket — §2.17). |

### 1.3 Untouched — the concealed wiki's entire visible content

Show, unchanged. This is rule 2, and getting it wrong in the *other* direction (over-concealing) is also a tell.

- `Location` and `Place` rows and every address proxy.
- Boundary source-candidate rows (`models/boundary/model.py:83`; schema-enforced provider-only via the `boundary_unique_source_candidate` constraint) and `boundary_vote.options[]`.
- Property Records card — the whole REData parcel/tax/lien/assessment payload (`plugins/builtin/property_records.py:463`), subject to the separate `SiteFeature.PROPERTY_OWNERS` entitlement gate, which is *not* concealment and must still apply.
- Building Attributes card (`plugins/builtin/redata_building_attributes.py:176`).
- Parcel Buildings rows: `name`, `building_number`, `year_built`, `source_label`, coordinates, geometry.
- All external provider media tiles (`services/apis/assets/base.py:55`) — Wikimedia, Smithsonian, LoC, Internet Archive, Digital Commonwealth, Yelp, Google, SearXNG, LoopNet, CRIS — plus server-rendered preview thumbnails (`services/media/previews.py:220`).
- `LocationCache` warmth / panel readiness. Verified safe: `prioritized_location_candidates` selects locations annotated `has_wiki OR has_pin` (`services/locations/enrichment.py:465-470`), and a draft wiki exists for every pinned location — so a warm cache is not evidence of *other* users.
- `boundary.pending` / `boundary.refreshing` (`external_api/views_wiki.py:560`) — reads `Location.place_resolved_at`, provider-run state only.
- `WIKI_STAT_FIELD_META` (`controllers/location_wiki.py:54`) — static labels. All four stat rows still render, each reading "No votes yet" with the viewer's own interactive stars.
- `first_pinned_precision` (`community_counts.py:117`) — hardcoded `"month"`.
- The viewer's own: `my_vote` on all four stats, `my_vote_id`/`is_my_choice` on boundary options, `is_relevant` media marks, own photos and their labels, own comments, own floorplan `versions[]` (`controllers/floorplans.py:216`, already profile-scoped), own detail pins (`Pin.parent_pin`, all endpoints scoped `profile__user=request.user`).
- **Write affordances.** Keep the comment composer, the add-alias form, the add-link dialog, the always-editable article canvas, the stat-vote widget, the boundary-vote button. A brand-new wiki offers all of these. Removing them is a tell. Remove only *per-row* controls (alias chip remove/use/toggle, link remove, comment delete, revision restore), which vanish with their rows.

### 1.4 Serializers and payload builders

| Site | Change |
|---|---|
| `services/wiki/wiki_detail.build_wiki_detail:119` | The single external-API assembly point. Every key below flows through it. |
| `wiki_detail.py:152` `security` | All eight `"unknown"`. |
| `wiki_detail.py:156` `cover_photo_url` | `null`. |
| `wiki_detail.py:161` `aliases[]` | Filtered set; `is_current` recomputed against the concealed name; stable non-pk ordering. |
| `wiki_detail.py:162` `links[]` | Provider set; `order` renumbered from 0 over survivors. |
| `wiki_detail.py:165` `comment_count` | Count of the viewer's own comments. |
| `wiki_detail.py:166-167` `created` / `updated` | Coarsened / `= created`. |
| `wiki_detail.py:72-93` `_article_summary` | Computed over the seed revision only, or `None`. |
| `wiki_detail.py:96-116` `_stats` | `rounded=None, exact=None, count=0` set **atomically**; `my_vote` preserved. |
| `wiki_detail.py:38-48` `_boundary_geojson` | Resolved with wiki rows excluded and with the zero-vote boundary winner (§2.19). |
| `external_api/views_wiki.py:669` `WikiOwnershipView` | Currently paginates `WikiOwner.objects.for_location()` **raw** — no `visible_owners`, no entitlement gate. Must apply both. |
| `external_api/views_wiki.py:686` `WikiPropertySalesView` | Same defect, same fix. |
| `external_api/views_wiki.py:743` wiki gallery | Empty page, `count: 0`. |
| `external_api/serializers.py:2286` `build_photo_payload` | 404 for a photo whose only route to the caller is a concealed wiki. |
| `external_api/views.py:1406` photo vote | `score` over the caller's own vote only; ideally 404. |
| `controllers/wiki_media.py:285` `WikiMediaVoteView` | Return only the viewer's own vote state; **do not materialise**; omit `image_id` / `image_url`. |
| `controllers/location_wiki.py:372-373` `about_html` | A second, independent render of `_wiki_about_card.html` that bypasses `LocationWikiView`'s context entirely. Must return `""`. |
| `services/floorplans/serialization.py:157` `version_token` | Not emitted. Do **not** synthesise one — a token that does not match the row makes every save fail `_reject_stale` with a spurious conflict. |

### 1.5 Templates and client JS

Templates must receive already-concealed context; none of these should contain a concealment branch. Listed because each is a place a payload-layer bug becomes visible.

- `partials/wiki/_wiki_about_card.html:12` — the card's render gate is a "has any" over four user-editable items. Recompute over the concealed set; when nothing survives, `#wiki-about-card` must be **absent**, not present-and-empty.
- `_wiki_about_card.html:34` — the `.security-indicators` block gate. Note a pre-existing defect: the security block lives *inside* the card gate at line 12, so a wiki with only security set renders no card at all today.
- `pages/location/wiki.html:421-450` — **the most easily missed leak on the surface.** The Suggest-edits dialog is in the initial HTML of every wiki page for every viewer, not lazy-loaded. It pre-fills `value="{{ wiki.name }}"`, the description textarea, both date inputs, and pre-selects the current `SecurityLevel` on all eight selects (`{% if sf_val == val %} selected{% endif %}`, line 447). Prefill from concealed values; do **not** suppress the dialog (its absence is its own tell).
- `pages/location/wiki.html:513-530` — the recently-viewed localStorage entry writes `wiki.name` into the viewer's browser and resurfaces it on the home-page widget, escaping the server render entirely. Write the concealed name.
- `pages/location/wiki.html:601-609` `sortByVotes()` — must be a no-op (§3).
- `partials/wiki/_boundary_vote_dialog.html:147-154` — the auto-open timer, gated on `boundary_vote.auto_open`.
- `partials/pins/pin_media_items.html:3` — the `debug` block. Inert on the wiki today (`WikiMediaProviderView` passes no `debug`), but the template is shared with pin detail, where `debug.query` is a user-authored pin name. Never render it on a wiki response, concealed or not.
- `frontend/ts/entries/map-annotations.ts:957-961` — `refreshPanelHeader` computes `total = detailPins.length + markupItems.length + photoPanelItems.length`, writes "N Items", and hides the edge handle on `total ? "" : "none"`. Client-side, after the payloads land. It falls out correctly *only if* concealment was applied at the payload layer. Same file, `:2252-2258`: `setMainMarkerVisible(!boundaryHasRealPolygon("property"))` and the one-shot `fitBounds` frame the map to a possibly community-drawn polygon.

### 1.6 Write-side suppression

Concealment that only filters reads leaks backwards. A concealed viewing must not write:

1. `controllers/location_wiki.py:113-115` — `viewed_by_other = True`. A concealed viewer's page load currently announces their visit to the creator, permanently, and retires the creator's Delete button. Suppress entirely.
2. `controllers/wiki_media.py:251,279` — `record_relevant_and_cache` materialises a provider item into a local `Image` row, and `queue_relevance_vote` pushes the vote to REData. Both create state that later leaks via `local_url` and `redata_confidence`.
3. Any `WikiEdit` written by a concealed viewer's action, and any `FactEvidence` (`models/wiki_edit/signals.py:32-52`).
4. `services/media/quota_rewards.py:98-110` — the community-contribution quota bonus must not be computed on a concealed viewer's request path.
5. **Existence oracles on write.** An alias add that 409s on a concealed duplicate (`controllers/aliases.py:233`) or a link add that 409s (`controllers/links.py:129`) answers the question directly. Concealed writes must succeed-or-noop, never conflict on concealed rows.

---

## 2. The MIXED problem

Per field: how provenance is (or is not) recorded, the three options, and a recommendation. Ordered by how much of the engineer's time it will consume.

### 2.1 `Wiki.name` — **no provenance. Add a column.**

`models/wiki/model.py:68`. The most important field on the surface.

Automatic writers: `tasks.py:103-124` (`enrich_wiki_location`, replaces a non-meaningful name with `location.official_name` or `PlaceNameResolverChain.resolve()`); `naming.py:190-196`; `wiki/queryset.py (draft/claim code, removed 2026-08-25)/181/213`. User writers: `wiki_edits.py:122-169`; `wiki_aliases.py:111` (`promote_wiki_alias_to_name`); `consensus/fields.py:70-71`; `wiki_creation.py:141-177` (`_name_from_pin` renames a freshly-claimed wiki to a user-chosen pin alias via a bare `wiki.save()` with no `WikiEdit`).

Four lossy channels, none authoritative:
1. **No `name_is_user_provided` column.** `Pin` has exactly this flag at `models/pin/model.py:121`. `Wiki` never grew one.
2. **`WikiEdit` with a `"name"` key** — but `LocationWikiEditDeleteView` (`controllers/location_wiki.py:453-468`) hard-deletes the edit *and* its revert record while leaving the value in place, and `wiki_creation.py:177` writes no edit at all.
3. **The matching `WikiAlias`** — `Wiki.save()` (`model.py:248-258`) get_or_creates an alias whose default source is `"user"`, while `naming.py:618-622` pre-creates provider-attributed `OFFICIAL` rows first. But `tasks.py:124` writes the enrichment name with a bulk `.update()` that bypasses `save()`, leaving **no** alias row.
4. **`FactEvidence` keyed `"wiki_name"`** (`facts/evidence.py:59`) — append-only, survives `WikiEdit` deletion, but only covers edits since Facts shipped.

**Recommendation: add `Wiki.name_is_user_provided`,** mirroring `Pin`'s, written `True` by all four user paths and honoured (`filter(name_is_user_provided=False)`) by all four automatic paths — which is exactly the discipline `pin_type_is_user_provided` already demonstrates. Backfill `True` where a surviving `WikiEdit`/`FactEvidence` names it, `False` otherwise (a false negative here shows a user name; accept it only if the backfill is auditable, otherwise backfill `True` where the name differs from `location.official_name` and the placeholder). **Until then: blanket-conceal** — always the automatic name. Cost: a wiki whose community name is genuinely identical to the official one still shows the official one, which is harmless.

Inherited by `slug`, `get_unique_search_name()`, `__str__`, `to_json()`, `undo` descriptions, `other_locations[].display_name`, `parent_wiki` link text, and the localStorage widget entry.

### 2.2 `Wiki.slug` — derived from §2.1, frozen

`model.py:65`, `_slugify_base()` at `:445`. Generated from `name` at first save and **never** regenerated on rename (`abstract/model.py:194-198` only generates when falsy; the only `ensure_slug` caller for a wiki is `models/pin/model.py:532`). Usually the automatic placeholder, but `wiki_creation.py:174-177` can rename moments after claim. **Recommendation: suppress `wiki_slug` from payloads,** or emit `slugify(concealed_name)`. Recoverable only by comparing to `slugify()` of the automatic candidates — not sound enough to gate on.

### 2.3 `Wiki.pin_type` — **provenance is clean. Use it.**

`model.py:74`, flag at `:78`. This is the one field the codebase gets right: every automatic writer honours the flag (`site_scope.py:273`, `site_scope.py:319-320`), every user writer sets it (`consensus/fields.py:84`, `detail_pins.py:457`), and `places/scope.py:156-157` reads it first. **Concealed:** `pin_type_for_place(place)` with the flag forced `False`. This is the pattern §2.1 and §2.5 are missing.

### 2.4 `Wiki.indoor_outdoor` — user-only today, MIXED by design

`model.py:86-92`. Only writer is `consensus/fields.py:78-79` (a player's game answer). The Facts registry admits non-user sources for key `wiki_indoor_outdoor` (`evidence.py:52,61`; `registry.py:60`), which is what makes it MIXED going forward. The Wiki column itself carries no flag. **Recommendation: conceal to `None`** (the default). Revisit only if an automatic writer lands, at which point filter `FactEvidence` by `source_kind`.

### 2.5 `Wiki.labels` (M2M) — **no provenance. Needs a through-model.**

`model.py:107-111`. Three writers with three provenances: `labels/auto_tag.py:148` (keyword + AI, queued from `models/wiki/signals.py:8-31`) = automatic; `labels/statuses.py:48-50` = automatic; `controllers/labels.py:1412/1417` = user. The M2M uses Django's implicit auto-created through table — `(wiki_id, label_id)`, no source, no timestamp, no actor. Only *removals* are recorded, as `WikiAutoRemoval` tombstones.

Worse: the AI matcher's prompt includes the wiki's user-written `description` verbatim (`auto_tag.py:554-555`), so a nominally automatic label can be a downstream function of concealed user text.

**Recommendation: blanket-conceal (empty).** The page renders no labels today, so the blanket rule costs nothing now — but declare a through-model with `source` + `applied_at` before any label UI ships on this surface, or the field becomes permanently unshowable.

### 2.6 `Wiki.updated` — **no provenance. Blanket rule now, column later.**

`abstract/model.py:47`. The single most direct "someone edited this" signal. `save_edited_fields` explicitly appends `"updated"` to `update_fields` on every community edit (`wiki_edits.py:77`); enrichment writes (`tasks.enrich_wiki_location`, `naming.py:775`, cover-photo saves at `image_gallery.py:409`) bump the same column with no counterpart row. `max(created)` over `WikiEdit` recovers the *user* edit times but there is no audit row for automatic writes, so an automatic-only `updated` is not reconstructible.

Note an inconsistency, not a protection: the markup-driven security write (`controllers/markup.py:76`) omits `"updated"` from `update_fields`, so that one path does not bump it.

**Recommendation: report `updated = created`.** Add `Wiki.last_automatic_update`, touched only by the enrichment path, when someone is in that code anyway. Because `updated` is `get_latest_by`, any ordering built on it leaks the same thing.

### 2.7 `Wiki.created` — automatic in origin, tracks the first pin

`abstract/model.py:46`. The draft row is created by `tasks.ensure_draft_wiki_for_location`, queued from the `Pin` `post_save` signal (`models/pin/signals.py:360-362`), so it dates the first human to pin the place — and `wiki_detail.py:166` ships it as a full ISO datetime while `first_pinned` right beside it is deliberately coarsened to the 1st of the month and suppressed below three pinners. **Every protection on `first_pinned` is defeated by reading `created` in the same response, today, for every API consumer, independent of concealment.** Fix regardless. **Recommendation: coarsen to the same month precision and suppress under the same rule.**

### 2.8 `WikiAlias` — **provenance recoverable via `source`, not `created_by`**

`models/aliases/model.py:52-55`, `:122-140`.

`created_by` is the intuitive discriminator and it is wrong: it is `NULL` for the geocoder backfill *and* for rename-created aliases (`Wiki.save()` `model.py:256`, `wiki_aliases.py:102` both `get_or_create` with `defaults={"name": ...}` only), and it is `on_delete=SET_NULL`, so a user alias becomes `NULL` when that account is deleted.

The durable discriminator is **`source`**: automatic rows carry the provider slug (`naming.py:618-622`), every user path leaves the model default `"user"` or the explicit `"wiki_sync"` (`models/aliases/signals.py:28,69`). It fails safe — a future writer that forgets to set it gets `"user"` and is concealed rather than leaked. The codebase already relies on exactly this test at `services/sharing/pin_sharing.py:178`.

One caveat: `Wiki.save()`'s `get_or_create` creates a `"user"`-attributed alias for whatever name it persists, so an automatic path going through `save()` gets a user-attributed alias unless `naming.py` pre-created the official row first — the ordering at `naming.py:177-178` exists to guarantee this.

**`kind` is a separate, unrecoverable question.** `toggle_nickname` (`aliases/model.py:80-88`) flips *any* alias to `NICKNAME` and demotes it to `ALTERNATE` on the way back, never restoring `OFFICIAL`, and its docstring states prior kind is not tracked. A provider alias showing `kind='nickname'` proves a person clicked it. **Recommendation:** filter on `source`, and force `kind` to `OFFICIAL`/`ALTERNATE` on survivors; never render `is_nickname` or the toggle's active state.

### 2.9 `WikiLink` — **the worst gap on the surface. Add a `source` column.**

`models/links/model.py:31-34,99`.

Automatic writers: `services/locations/external_links.py:76` (`add_wiki_link`), reached from `models/cache/signals.py:67` (Wikipedia), `plugins/builtin/nominatim.py:200` (OSM), `plugins/builtin/epa_echo.py:369` (EPA). All three leave `created_by` unset. User writers: `controllers/links.py:128`, `external_api/views_wiki.py:721`, both `created_by=profile`.

`created_by` is the **only** signal and it is `on_delete=SET_NULL`. When a contributor deletes their account, their hand-typed link becomes byte-identical to a provider-discovered one and silently reclassifies from concealed to shown. Unlike `WikiAlias` there is no `source`; unlike `WikiEdit` there is no other attribution for the row. The three automatic writers use fixed display names (`"Wikipedia"`, `"OpenStreetMap"`, `"EPA Compliance Report"`) that a user can trivially imitate.

**Recommendation: add `WikiLink.source`, mirroring `WikiAlias.source` exactly** — same default, same semantics, same fail-safe. Small migration, and it retires the whole problem. **Until then: do not filter stored rows.** Re-derive the automatic link set from the enrichment sources (the `LocationCache` `"wikipedia"` URL, the Nominatim OSM URL, the EPA URL) and render that, ignoring the `WikiLink` table. This also happens to fix the `WikiAutoRemoval` absence leak (§2.20) for free.

`wayback_url` (`model.py:33`) is automatic (`models/links/signals.py:23-34`) but exists only because a parent link exists — it inherits, and must never be the reason a row is shown.

### 2.10 `Article.content` / `content_html` / `last_edited_by` — **recoverable via the revision chain**

`models/article/model.py:51,54,73`.

An article can be legitimately automatic: `services/wiki/wiki_seed.py:83` seeds a wiki's first article from a confidently-matched Wikipedia extract with `editor=None`. Blanket-hiding articles would itself be a tell.

The Article *row* records nothing about provenance — `save_article` overwrites `content` in place (`services/wiki/articles.py:306-310`), and `last_edited_by=NULL` is ambiguous between "system-seeded" and "editor's account deleted" (documented at `model.py:183-203`).

The **revision chain** answers it. `EDIT_SUMMARY_SEEDED_FROM_WIKIPEDIA` / `SYSTEM_EDIT_SUMMARIES` (`model.py:36-37`) exist precisely for this and are already used by `ArticleRevision.editor_display_name`. A revision is automatic iff **`editor_id IS NULL` AND `edit_summary IN SYSTEM_EDIT_SUMMARIES`** — pair both tests, because a user can type the literal string "Seeded from Wikipedia" into their own summary, but cannot produce a NULL editor. The seed revision is not user-deletable: the self-service delete filters `editor=profile` (`external_api/views_wiki.py:977`), which a NULL editor never matches.

**Recommendation: use it.** Concealed article = the newest system-authored revision, re-rendered on read via `render_article()` (never the cached `content_html`), with `word_count` and `toc` recomputed over it, `last_edited_by=None`, `updated` = that revision's `created`, `base_revision_id` = that revision's id. No system revision ⇒ present the "This place doesn't have an article yet" empty state (`_article_panel.html:23-39`), which is verbatim what a fresh wiki with no Wikipedia match shows.

### 2.11 `Image.wiki` — **provenance is `profile_id IS NULL`, and it is already unusable**

`models/images/model.py:133`. Three actors set it: (a) background enrichment (`services/photos/photo_enrichment.py:91-99`, called at `:195/:231/:260` for Google Places business photos, Street View, Satellite) with **no profile**; (b) a person uploading (`services/photos/uploads.py:129`) or bulk "Send to wiki" (`controllers/image_gallery.py:209-223`); (c) materialize-on-upvote (`services/media/media_materialize.py:293-310`), where the profile stamped is the **up-voter**, not the photographer.

`profile_id IS NULL` is a sound test and the codebase already names it as such — `management/commands/export_public_locations.py:96-98`: *"profile__isnull=True is the whole test for 'nobody authored this'"*.

**The complication, confirmed by reading `models/images/queryset.py:138-140`:** `visible_to` returns `Q(profile=viewer) | _named_this_viewer(viewer) | (Q(profile_id__in=allowed_uploader_ids) & _shared_within_reach_of(viewer))`. A NULL `profile_id` matches none of the three — `profile_id__in=<set of ints>` can never match NULL. **Enrichment photos are already filtered out of every wiki gallery, the map layer, the API gallery, and `MediaGateView._authorize_image`.** So the "plausible new-wiki photo baseline" is empty today, and the only wiki photos anyone can see are user-contributed ones.

**Recommendation:** treat the concealed wiki photo set as **empty**, and the `photos` media panel as a permanent 204 (`controllers/wiki_media.py:153`). Separately, decide whether `visible_to` should admit ownerless rows — that is a privacy-model question, not a concealment one, and it changes the baseline for everyone. Do not couple the two changes.

### 2.12 `Image.source` — **looks like provenance, is not**

`models/images/model.py`. `GOOGLE_MAPS` is written by enrichment (`photo_enrichment.py:195`) *and* by user-driven materialisation (`media_materialize.py`). `WIKIMEDIA`/`SMITHSONIAN`/`LOC`/`YELP` only ever exist because a user up-voted. `UPLOAD`/`FLICKR` are always user acts. **Recommendation: never classify on `source`.** Use `profile_id IS NULL`, optionally corroborated by `media_source_key IS NULL` (materialize always sets it; enrichment never does — the cleanest positive "a user caused this row" marker in the schema).

### 2.13 `Image.caption` / `author` / `copyright` / `source_url` — no per-field source

`model.py:196,203,213,204`. Three writers each: user-typed at upload (`uploads.py:132`), EXIF/IPTC back-fill (`services/media/images.py:290,312,330,352`), provider strings (`photo_enrichment.py:96`, `media_materialize.py:306`). `author` is the worst: per the model comment at `:197-202`, when a photo has no author/source_url/caption/copyright and its filename matches a phone auto-naming pattern, **the uploader is assumed to be the author and written here** — a user identity synthesised into an attribution field, with the `is_camera_generated_filename` heuristic (`images.py:380`) not recorded on the row. `source_url` can be an Immich/Google Photos/Flickr account asset URL linking into a named user's personal library (`tasks.py:1261,1938,2038`).

**Recommendation: blanket rule at the row level** — suppress all four on any row with a non-null `profile`. If `author` ever needs to be shown on a user row, it needs an `author_source` column first.

### 2.14 `Image.latitude` / `longitude` — **the model documents its own gap**

`model.py:234-247`. One pair of columns holds two provenances — what EXIF reported, and where a person dragged the marker (`controllers/image_gallery.py:425`) — *"with nothing in the schema recording which a given row holds"*, with an existing TODO for `exif_latitude`/`exif_longitude` or a `coordinate_source` field. Worse, `tasks.process_image_upload` (`tasks.py:853-858`) rewrites them unconditionally from EXIF, so a manual correction silently reverts.

**Recommendation: blanket.** No photo coordinates on a concealed wiki; `WikiGalleryJsonView` returns `{"images": []}`. This is the single most disclosive item on the media surface — markers scattered across a building interior are a visit log. `estimated_latitude`/`estimated_longitude` (SpotGuessr crowd estimates, `model.py:257`) and `coordinates_are_estimated` follow: their non-nullness alone proves the photo was played enough times to accumulate guesses.

### 2.15 `Wiki.cover_photo` — the *act* is never recorded

`model.py:173-179`. The pointed-at Image's provenance is recoverable via `profile_id`, but nothing records that the FK was set automatically **because it never is** — `WikiCoverPhotoView` (`image_gallery.py:389-409`) is the only writer, and `image_gallery.py:400-401` notes the cover is rendered with no visibility gate of its own. A brand-new wiki has `cover_photo=NULL`. **Recommendation: treat as USER, always.** No hero banner, `cover_photo_url: null`, no candidates carousel. Do not substitute an enrichment photo.

### 2.16 Child wikis (`detail_pins[]`) — **no recoverable provenance. Needs a column.**

Two routes write the *same columns with the same values*:
- User: `controllers/detail_pins.py:377-390`.
- Automatic REData building seeding: `services/wiki/wiki_creation.py:119-130` → `services/pins/pin_restructure.py:589-596`.

Every plausible discriminator fails:
- `created_by` is NULL on both (neither path passes it; the only writers are `wiki/queryset.py (draft/claim code, removed 2026-08-25),222` for top-level wikis).
- `pin_type_is_user_provided` is `False` on both — user placement with the dialog's "Auto" option writes `_PROVISIONAL_PIN_TYPE` with the flag `False`, then `classify_detail_marker` can flip it to `BUILDING` while leaving the flag `False` (`tasks.py`). The flag means "an editor chose this type", never "a human created this row".
- `place_id` fails in **both** directions: `place_by_key.get(...)` returns `None` for a building the provisioner could not reconcile, so seeded rows can also be placeless (`pin_restructure.py`), and nothing back-fills a child wiki's `place` later.
- The paired `WikiEdit` differs (`child_wiki_added` vs. one aggregate `child_wikis_imported`) but the import entry names only a count, not which rows.

One *one-directional* signal does hold: seeding sets only `name` and leaves `description`/`icon`/`color`/`detail_bg_color`/`detail_border_color` at defaults. So **non-default styling or a non-empty description proves user contribution** — but the converse does not hold.

**Recommendation: add `Wiki.source` (or `is_user_provided`), written at creation by both paths.** Until then, **blanket-conceal** `detail_pins: []`, accepting that this wrongly hides the REData buildings rule 2 says should show. Consequences of the blanket rule that must be handled together: pass `children=[]` to `parcel_buildings.building_rows` (§3), and force `building_child_count` to 0 in scope resolution (§3).

### 2.17 Boundary `polygon` vs `generated_polygon` — **the model to copy**

`models/boundary/model.py:83-90,101`. Two provenances in two separate columns on the same row, `source` naming the provider, `generated_at` recording the last provider run, and official geometry kept off the table entirely on `Place.geometry`. Verified: no writer of `generated_polygon` ever targets a wiki row (`services/places/provisioning.py:418-420` targets place rows, `services/geo/child_pin_boundaries.py:77-91` targets pin rows), so a wiki-keyed `Boundary` can only ever hold a hand-drawn polygon. **Recommendation: exclude wiki rows from resolution** and let the chain fall through to `place` → `circle`. Recompute `boundaries.<type>.source` together with the polygon — never patch the string alone, or the two disagree.

### 2.18 `Place.geometry` after `apply_winning_boundary` — provider bytes, community choice

`services/geo/boundary_voting.py:209-210`. The geometry is unimpeachable provider data, but *which* provider's polygon is showing can be a community decision. The vote is not recorded on the `Place` — only the materialised geometry is. **Recommendation: recompute the zero-vote winner** — `min(boundary_options(place), key=_priority).generated_polygon` (REData, then Overpass, then pk; `boundary_voting.py:61-64,98-100,151`). Deterministic, needs no vote rows. Do not simply show `Place.geometry`.

### 2.19 `WikiOwner` — `source` exists, but the OFFICIAL row sometimes never materialises

`models/property_owner/model.py:78`. `source` is durable: REData writes `OFFICIAL` (`plugins/builtin/property_records.py:342`), the wiki panels write the `USER` default (`controllers/property_owner.py:373,458`). Filtering works mechanically.

**The collision:** `_get_or_create_official_owner` (`property_records.py:336`) returns an existing case-insensitively matching `WikiOwner` instead of creating its own `OFFICIAL` row. So when a community member types `ACME HOLDINGS LLC` before the REData fetch runs, the row stays `source=USER` forever even though county records report the identical name. Concealing all USER rows then drops a name a brand-new wiki *would* have shown — the concealed page **under-reports** relative to the honest baseline, detectable by anyone who cross-checks the deed. The same defect corrupts sale party names (`property_records.py:395`).

Related: `WikiPropertySaleTabView.post` unlinks *every* previous owner when a user records a sale (`controllers/property_owner.py:464-466`), **not restricted to USER rows** — so a community sale can remove an OFFICIAL owner, and the absence is user-caused negative space with no audit row.

**Recommendation: rebuild the visible owner and sale-party sets from the cached `property_records` payload's `owner_name` / `sales_history`, not from `WikiOwner` rows.** Independently, fix `_get_or_create_official_owner` to create its own OFFICIAL row and dedup at render time. `WikiPropertySale.source` itself is clean and can be filtered directly.

### 2.20 `WikiAutoRemoval` — a leak by **omission** that no row filter catches

`models/auto_removals/model.py:84`. When a user deletes an auto-added link/alias/label, a tombstone is written (`controllers/links.py:146`, `controllers/aliases.py:253`, `controllers/labels.py:1416`) and every automatic re-add path then refuses to recreate it (`external_links.py:69`, `naming.py:615`, `auto_tag.py:172`). Result: a provider link a fresh wiki would show is **missing**, precisely because a user removed it.

**Recommendation: under concealment, ignore `WikiAutoRemoval` entirely** when assembling the automatic link/alias/label sets. Falls out of the §2.9 recommendation to re-derive links from enrichment sources rather than filter rows.

### 2.21 Floorplan pools and `FloorplanItem.attributes` — unbounded, unjudged

`models/floorplans/model.py:174-206,278-341`. A `FloorplanSource` can be a scanned HABS sheet (rule 2) or `note="measured on site, 2019 visit"` with `author` naming a person. There is no `kind`/`is_external` column; `FloorplanReference.kind` describes media type, not provenance. `attributes` is an explicitly unmerged producer-defined JSON blob with no key-level rule that could be written against it. **Recommendation: withhold with the community document.** If a plan is ever partially shown, drop `attributes` wholesale — and note that item rows point into the pools by uuid (`serialization.py:57-58`), so a pool entry cannot be removed without clearing its references.

The `Floorplan` itself is clean: `wiki_id IS NOT NULL` selects exactly the community plans, and `Floorplan.objects.at(place, community=True)` already filters on it. `origin` (`services/floorplans/resolution.py:55`) is a first-class provenance value — recompute it rather than string-patching, so the editability the client infers matches what the server will accept.

### 2.22 `Fact` / `FactEvidence` — provenance exists, answer is currently always "user"

`models/facts/model.py:151,256-261`. Every wired writer today is a user action (SpotGuessr `photo_coordinates.py:58`, Consensus `session.py:454`, `wiki_edit/signals.py:32-52`); `record_ai_evidence` is documented as "a ready seam, not called anywhere yet" (`evidence.py:296`) and `EXTERNAL_SOURCE` has no writer. Nothing renders facts on the wiki today. **Recommendation: blanket-conceal now**, and when non-user evidence lands, filter per-evidence by `source_kind` and recompute `confidence`/`status`/`evidence_count` over survivors.

---

## 3. The DERIVED worklist

Every value computed over other rows. Each must be recomputed over the **concealed** set, not blanked.

**Counts and badges**

| Item | Call site | Concealed |
|---|---|---|
| `wiki_comment_count` (tab badge) | `controllers/location_wiki.py:189` | Own comments only → 0; badge element **absent** (existing `{% if %}` guard). Note it is raw and ungated today, already exceeding what a viewer may read. |
| `comment_count` (API) | `services/wiki/wiki_detail.py:165` | Same. |
| `total_comment_count` (panel) | `controllers/comments.py:242` | Same; same pre-existing over-count defect. |
| Alias count badge | `partials/pins/aliases_panel.html:7` | Length of the filtered list; hidden when empty. |
| Ownership count badge | `partials/pins/_ownership_panel.html:11` | Over OFFICIAL-only list. |
| Parcel Buildings count | `partials/pins/_parcel_buildings_panel.html:31` | Over the `children=[]` row set. |
| Albums count | `partials/albums/_albums_panel.html:21` | Absent (zero albums). |
| Manage-tab photo badge | `partials/pins/_photo_gallery.html:19` | Enrichment rows only → hidden. |
| Media count badge | `pages/location/wiki.html:613-621` | Provider tiles only. |
| Per-source tab counts | `pages/location/wiki.html:636-641` | No `photos` tab; "All" excludes it. |
| API gallery pagination `count` | `external_api/views_wiki.py:802` | 0. |
| Album `photo_count` / `placed_count` | `controllers/albums.py:152-157,173,227` | Own photos only; album absent otherwise. |
| Reply-count chip | `partials/comments/_comment_body.html:124` | Unreachable. |
| Revision ordinal `#N` | `controllers/article.py:316,329` | Over the visible revision set → `#1` on a seeded-only article. Also appears in the restore-confirm prose. |
| `wiki_editor_count` ("could notify N people") | `services/visits/safety.py:1673` | 0. Verify the toggle copy matches an actually-unedited wiki; if hidden at zero, hide it. |
| `#detail-pin-count-label` / edge handle | `frontend/ts/entries/map-annotations.ts:957-961` | Falls out from empty payloads. |

**Aggregates and scores**

| Item | Call site | Concealed |
|---|---|---|
| `stats.<field>.rounded` / `.exact` / `.count` | `models/wiki_stat_vote/queryset.py:69,78,95` | `None`/`None`/`0`, set **atomically**. |
| **Live defect:** filled stars survive the count fuzz | `queryset.py:88` vs `_wiki_stat_rating_item.html:20,23` | `composite()` zeroes `display_count` when `is_low` (<3 ballots), but `rounded`/`exact` are the real values. The template branches the *label* on `count` and the *stars* on `rounded`, so a 1–2-vote wiki renders lit stars directly above the text "No votes yet". Present today, before any concealment layer. |
| `pin_count_approx` / `pin_count_low` / `first_pinned` | `services/wiki/community_counts.py:103-117` | `None` / `True` / `None`. Do **not** reuse `approximate_pin_count`'s floor as the gate — it is a fuzz, and reports "about 12" happily. |
| Media tile `vote_score` | `controllers/wiki_media.py:96,111,283` | 0 for every tile (or the viewer's own ±1). Keyed by `Location`, so votes cast on any user's *pin* page count here. |
| `has_consensus` (boundary) | `services/geo/boundary_voting.py:288`, `controllers/location_wiki.py:531` | `False` in context **and** in the vote-cast JSON response. |
| `has_votes` | `boundary_voting.py:287` | `False`. |
| `MediaRelevance` community aggregate on API vote | `external_api/views.py:1406` | Caller's own vote only; ideally 404. |
| `withheld_official_count` / `parties_withheld` | `services/property/owner_access.py:126,130` | Computed over the **entitlement** filter only, never over the concealment filter — concealment must produce a list that reads as complete. |
| Fact `evidence_count`/`confidence`/`status` | `models/facts/model.py:167` | Over the empty evidence set. |

**Orderings**

| Item | Call site | Concealed |
|---|---|---|
| Media grid sort | `pages/location/wiki.html:601-609` `sortByVotes()` | No-op for a concealed viewer. Zeroing the score is not enough — the permutation itself encodes the ranking against a provider's known native order. |
| `_photos` panel sort key | `controllers/wiki_media.py:149,161` | Drop **both** `vote_score` and `redata_confidence`; order by `created`/pk. `redata_confidence` is fed by community votes pushed via `queue_relevance_vote` (`wiki_media.py:279`), so it is a laundered vote aggregate. |
| `WikiLink.order` | `models/links/model.py:34,40`; `wiki_detail.py:162` | Renumber survivors from 0 — gaps reveal removals. |
| Alias list ordering / pks | `external_api/views_wiki.py:393`; `serializers_wiki.py:102` | Order by `name`; do not publish pks (sequential pks disclose insertion order and interleaving). |
| `ArticleRevision.size_delta` | `models/article/model.py:205`; `controllers/article.py:329` | Against the previous **visible** revision. A `+1,840` whose predecessor is invisible announces the invisible edit. |

**Timestamps**

`wiki.created` / `wiki.updated` (§2.6, §2.7); `article.updated` → seed revision's `created`; `Comment.created` (absent); `ArticleRevision.created` (seed only); `last_wiki_edit` (`services/visits/safety.py:1672` → `None`, template already renders "never"); `Album` `date_start`/`date_end` (`services/photos/albums.py:311` — a published range of when people were physically present); `Image.taken_at`; `WikiDeviceMarker.first_observed_at`/`last_observed_at` (`models/device_scan/model.py:236-242` — explicitly the *visits*, not the record); `Floorplan.version_token` (`serialization.py:157`).

**Booleans, gates and inversions**

| Item | Call site | Concealed |
|---|---|---|
| `boundary_vote.auto_open` | `services/geo/boundary_voting.py:289` | **`True`** — computed over *other people's* votes only. `auto_open = not has_votes`, rendered as a `{% if %}` around a real 800 ms auto-open timer (`_boundary_vote_dialog.html:147-154`). A user needs no devtools to notice the dialog pops at untouched places and stays shut at visited ones. Concealing `has_votes` without flipping this makes the leak *worse*. |
| About card render gate | `partials/wiki/_wiki_about_card.html:12` | Over the concealed field set; card absent entirely when nothing survives. The client-side swap at `wiki.html:314-322` must receive `""`, not a stripped card. |
| Security block gate | `_wiki_about_card.html:34` | `False`. |
| `is_site_scope` → Building Attributes 204 | `controllers/location_wiki.py:238`; `services/locations/site_scope.py:141` | Compute with `building_child_count` forced to 0. Otherwise two community building markers make a **provider** card vanish, and its absence is the tell. |
| `scope_badge` | `services/places/scope.py:137`; `controllers/location_wiki.py:188` | From `pin_type_for_place(place)` alone; no badge for the neutral default (`scope.py:138-140`). |
| `can_delete_wiki` + its title text | `controllers/location_wiki.py:186`; `models/wiki/model.py:302-316` | For the creator, evaluate as if `viewed_by_other` were `False`. The button's disappearance, and the literal strings *"No one else has viewed this wiki yet, so you can still delete it"* / *"No one else has viewed it yet"*, are direct announcements. |
| `show_wiki_cover_photo` / `wiki_cover_candidates` | `controllers/location_wiki.py:157-162` | `False` / `[]`. The prev/next arrows render on `{% if wiki_cover_candidates %}` (`wiki.html:41,58`), so the arrows alone are a ">1 photo" boolean. |
| Photos panel 200 vs 204 | `controllers/wiki_media.py:153` | Always 204. The status code alone answers the question. |
| `local_url` substitution / `image_id` on a provider tile | `controllers/wiki_media.py:101,120` | Always hot-link the provider's `item.url`; `image_id` always empty. Documented in-code as *"only present once this item has a local copy"* — i.e. a "somebody materialised this" boolean. Scoped by `Location`, so a vote on any pin page leaks here. |
| Parcel Buildings out-of-boundary bypass | `plugins/builtin/parcel_buildings.py:374` | Passing `children=[]` makes the boundary filter apply uniformly. Otherwise the mere *presence* of an extra row proves a marker was placed there — a `child_name`-only fix misses this. |
| Ownership card `data-collapse-if-empty` | `partials/pins/_ownership_panel.html:7` | From the concealed list. |
| `gallery-has-coords` badge | `partials/pins/_photo_gallery.html:79` | Never rendered. |
| `on_wiki` / `uploaded` / `is_mine` flags | `services/media/images.py:731,739,740` | `False` / not emitted / own-only. `on_wiki` is reachable from the pin gallery and profile photo strip, so it leaks contribution status from *outside* the wiki. |
| Revision "current" chip / restore button | `controllers/article.py:365` | Over the visible set, so the newest visible revision reads as current. |
| `parent_deleted` / `map_removed` tombstones | `models/comments/model.py:69,77` | Absent. These survive the rows they describe. |
| `lock_state` on a floorplan opening | `services/floorplans/features.py:198`, `:83-103` | `"unknown"` — the value `_opening_state` returns for empty `states`. Survives hiding the `locks[]` array. |
| `blurred_profiles` | `controllers/comments.py:210` | Empty set. |
| `quota_exempt_reason=COMMUNITY_CONTRIBUTION` | `models/images/model.py:287`; `services/media/quota_rewards.py:50-110` | Never reaches a concealed viewer; the bonus computation must not run on their request path. |

**Empty states that are the correct concealed rendering** (route to these, not to empty containers): comments panel "No comments yet / Be the first to share something about this place" (`partials/comments/comment_panel.html:31-43`); article "Be the first to document this place for the community" (`_article_panel.html:23-39`); article history "No article revisions yet" (`_article_history.html:50-55`); wiki-edit history "No edits have been recorded yet"; albums "No photos on this wiki yet" (`_albums_panel.html:86-90`); custom layers "No custom layers yet - add one below" (`_custom_layers_list.html:52`); photo gallery empty state (`_photo_gallery.html:109`); stat rows "No votes yet".

**Anti-pattern:** the links row renders a literal "No links yet." when empty (`_pin_links_row.html:53-54`). Combined with an About-card gate evaluated over *raw* links, that produces "the card exists because links exist, and the links row says there are none" — a direct signal that something was withheld. Compute the gate over the concealed list.

---

## 4. Chokepoints

Do not implement this fifty times.

**4.1 One module: `services/wiki/concealment.py`.**

```
concealment_active(wiki, location, viewer) -> bool     # §0.1; cache per-request
ConcealedWiki(wiki, viewer)                            # read-only presentation proxy
conceal_rows(qs, wiki, viewer)                         # the queryset AND-term
```

**4.2 One funnel: `resolve_visible_wiki` (`services/wiki/wiki_access.py:356`).** 98 call sites across every wiki-scoped controller and the external API — `controllers/{location_wiki,comments,article,aliases,links,markup,custom_layers,map_overlays,boundary,detail_pins,albums,image_gallery,wiki_media,property_owner,labels,flickr}.py`, `external_api/views_wiki.py`, `services/wiki/wiki_detail.py`, `services/pins/pin_detail.py`. Its docstring already states the rule that *every* wiki-scoped controller must resolve through it, and `tests/hypothesis/test_cross_user_route_access.py` and `test_controller_object_scoping.py` already police that class of invariant.

Have it return `ConcealedWiki` in place of `Wiki` when the predicate fires. It is a transparent read proxy delegating `__getattr__` to the real row, overriding only the §1.1 table plus `to_json`, `to_detail_json`, `get_unique_search_name`, `__str__`. **Make it raise on `save()` / `delete()` / any manager write.** That is not defensive tidiness — it is the mechanism that turns §1.6's write-side leaks into loud test failures instead of silent ones. Keep the tuple arity at 3 so no caller changes.

**4.3 One queryset term, on the existing `for_wiki()` family.** `models/markup/queryset.py:42,96`; `models/map_overlay/queryset.py:17`; `models/comments/queryset.py:22`; `models/wiki_edit/queryset.py:13`; `models/album/queryset.py:29`; `models/boundary/queryset.py:125`; `models/facts/queryset.py:25`; `models/wiki_stat_vote/queryset.py:46`; `models/device_scan/queryset.py`; `models/property_owner/queryset.py:36,74`; `models/reputation/queryset.py:69`; `models/consensus/queryset.py:132`.

These all currently take `(self, wiki)` with no viewer. Add `for_wiki(wiki, *, viewer=None)`, defaulting to today's behaviour, and have it apply `conceal_rows`. Passing `viewer` becomes the thing a reviewer greps for.

**4.4 Composition with the existing per-viewer filters.**

Concealment is a **third, independent conjunct**, applied after the two gates `docs/PRIVACY_MODEL.md` defines. Never modify the existing filters:

- `ImageQuerySet.visible_to` (`models/images/queryset.py:85`) — the settings gate. Concealment ANDs on top: `.visible_to(viewer).conceal_for(wiki, viewer)`. Do **not** widen `visible_to` to admit ownerless rows as part of this work (§2.11) — that is a separate privacy-model decision affecting every viewer.
- `visible_wiki_location_ids_cached` / `location_visible_to` (`wiki_access.py:212,257`) — the container gate. Answers *whether the page renders*. Concealment answers *what it contains*. Keeping them separate is what stops concealment from accidentally becoming an access-control bypass.
- `resolve_visible_identities` / `masked_editor_name` (`wiki_detail.py:51-69`) — masks **who**, never **that**. A masked name still proves a person edited. Concealment must remove the row, not mask the name. Do not mistake existing masking for existing concealment anywhere on this surface.
- `visible_owners` / `sale_rows` (`services/property/owner_access.py`) — the *entitlement* gate (`SiteFeature.PROPERTY_OWNERS`). Orthogonal; both must hold; `parties_withheld` must be computed over entitlement only.

**4.5 The layer rule.** Concealment lives in the payload/context/queryset layer. **Never in a template.** Four routes prove why: the second `_wiki_about_card.html` render at `controllers/location_wiki.py:372-373`; `Wiki.to_json()` / `to_detail_json()`; the raw external-API owner and sales endpoints at `external_api/views_wiki.py:669,686`; and the localStorage write at `pages/location/wiki.html:513-530`. A template-layer fix is bypassed by all four.

**4.6 Add a structural CI check**, alongside the three that already exist: every `for_wiki`/`for_location` call inside a request path must pass `viewer`, and no template may reference a raw wiki attribute in the §1.1 table. Cheap, and it is what keeps this from decaying.

---

## 5. What you would get wrong

Five places a careful engineer following this document still leaves a tell.

### 5.1 The shared fuzz cache hands a concealed viewer the real number

`services/wiki/community_counts.py:36,54-61`. `approximate_pin_count` caches its fuzzed value for 24h keyed **only** on the id passed in — no viewer identity in the key. Both the pinned-user count (`wiki.pk`) and all four stat composites (synthetic negative ids, `wiki_stat_vote/queryset.py:86-88`) share that namespace.

If you implement concealment by filtering the *input queryset* per viewer and still calling `composite()` / `wiki_community_summary()`, the concealed viewer's call **hits the cache entry a normal viewer populated** and is handed the real fuzzed number. The entire concealment is defeated by cache reuse, silently, and only under concurrency.

**The concealed path must short-circuit before `approximate_pin_count` is ever called**, returning the empty state directly. Do not re-key the cache; do not reuse it.

**Test:** normal viewer loads the wiki (populating the cache), then a concealed viewer loads it in the same 24h window, asserting `count == 0` and `pin_count_low is True`. Run it in both orders. This test does not exist and will not be written by accident.

### 5.2 Inverted tells — you conceal the fact and reveal it by the concealing

Four of these, all in the "quiet where a fresh wiki is loud" direction:

1. **`boundary_vote.auto_open`** (`boundary_voting.py:289`). Suppressing the dialog is the *wrong* fix. A fresh wiki auto-opens it; a concealed one must too. And for a viewer who has voted themselves, `has_votes` is true from their own row, so `auto_open` must be computed over **other people's** votes only or it contradicts itself for exactly that viewer.
2. **`is_site_scope` → Building Attributes 204** (`controllers/location_wiki.py:238`). A concealed viewer who sees a *provider* card missing has been told that ≥2 community building markers exist. Concealing the child markers does not fix this; forcing `building_child_count` to 0 does.
3. **`WikiAutoRemoval`** (§2.20). The concealed page must show the OpenStreetMap and Wikipedia links a fresh wiki would have, even though a user deleted them.
4. **`WikiOwner` dedup collision** (§2.19). Concealing all USER rows drops a name the county record confirms — the concealed page under-reports.

**Test:** build two wikis at the same place-type — one with contributions, one with none — run both through the concealed renderer, and diff the rendered HTML and every JSON endpoint. They must be identical modulo ids and coordinates. This "twin-wiki diff" is the only test that catches the whole inverted-tell class, and it is worth building as a fixture harness rather than as individual assertions.

### 5.3 You zero the scores and forget the ordering

`pages/location/wiki.html:601-609` (`sortByVotes()`) re-lays the media grid by `data-vote-score` after every provider swap; `controllers/wiki_media.py:149,161` sorts server-side on `(vote_score, redata_confidence, created)`.

Setting every `vote_score` to 0 leaves the *permutation* intact if the sort is not also disabled, and leaves `redata_confidence` — which is fed by community votes pushed to REData (`wiki_media.py:279`) — as a live secondary key. A viewer who knows Wikimedia's native result order can read the community ranking off a grid where every displayed number is zero.

The same failure mode in miniature: the stat-star defect (§3) shows that this codebase has *already* shipped a bug of exactly this shape — the count was fuzzed, the stars were not, and the two render side by side.

**Test:** assert the concealed media grid's DOM order equals the provider loader's arrival order, byte for byte, against a fixture where the community ranking is a known non-identity permutation. And assert `rounded`, `exact` and `count` are `None/None/0` as a single tuple, not individually.

### 5.4 The read is clean and the write leaks backwards

`viewed_by_other` is the sharp case: a concealed viewer's page load currently flips it to `True` (`controllers/location_wiki.py:113-115`), which permanently retires the creator's Delete button and thereby tells the *creator* that someone was here. The concealment protected the visitor and betrayed them in the same request.

The same shape: up-voting on a concealed wiki materialises an `Image` row (`wiki_media.py:251`) that later leaks via `local_url`; a concealed viewer's edit writes a `WikiEdit` and a `FactEvidence`; an alias or link add 409s on a concealed row.

**Test:** wrap a concealed page load and every concealed-viewer POST in an assertion that no write touches `Wiki`, `WikiEdit`, `Fact`, `FactEvidence`, `Image`, or `MediaRelevance` for rows the viewer does not own. The read-only proxy (§4.2) makes the `Wiki` half fail loudly; the rest needs an explicit query-capture test. Also assert that a duplicate alias/link add returns the same status as a fresh one.

### 5.5 You fix the page and forget the four other doors

The wiki page is not the only route to this data. Each of these bypasses a page-level fix entirely:

- `controllers/location_wiki.py:372-373` — the `about_html` re-render, which does not go through `LocationWikiView`'s context at all, and which `wiki.html:318-321` will use to **insert** the About card where none existed.
- `external_api/views_wiki.py:669,686` — owner and sales endpoints that apply no filter of any kind, not even the entitlement gate the web UI enforces.
- `services/global_search/providers.py:382,422` — a concealed wiki's photos remain findable by caption, author, keyword, OCR text and capture-date range, with the hit rendering the capture month. `visible_to` is applied, but that is a photo-visibility gate, not a concealment gate. Concealed-wiki photos must be excluded from the provider's **candidate set**.
- `pages/location/wiki.html:513-530` — the localStorage write that resurfaces a concealed name on the home-page widget, days later, with no server involvement.
- `Wiki.to_json()` / `to_detail_json()` — reachable from map payloads elsewhere in the app.

**Test:** enumerate every URL that resolves through `resolve_visible_wiki` (98 call sites; generate the list from the urlconf rather than by hand), hit each as a concealed viewer against a contributed wiki, and assert the response contains none of a set of canary strings planted in the fixture's user content — description text, alias name, comment body, uploader username, article heading, link URL, lock `key_attributes`. Then hit the global search provider and the home-page widget with the same canaries. A single grep-for-canaries harness across all routes catches more of this class than any amount of per-field review.