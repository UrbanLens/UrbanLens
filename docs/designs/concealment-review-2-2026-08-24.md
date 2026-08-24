# Review: the wiki concealment layer (d27d82ac..fb8b5fa5)

**Second adversarial review, 2026-08-24.** 102 findings survived verification - more than the
first review, on roughly the same volume of code. The headline: six of fourteen test assertions
did not exercise the code they name, which is why a self-review reported the layer clean.


**Read this first:** `concealment_active` (`services/wiki/concealment.py:151`) returns `False`. Nothing below leaks in production today. Every "leak" is a statement about the day that boolean flips, which is exactly the day nobody will re-derive any of this. The docstring at `:151-186` is the artefact whoever flips it will read, and it is wrong in every particular (§5).

---

## 0. What has been acted on since (2026-08-24, later the same day)

§1's architectural verdict was accepted and the layer reworked to what
`concealed-wiki-spec.md` §4.2 specified.

- **`conceal_wiki` no longer returns a proxy.** It returns a `copy` of the row
  with substituted field values - a real `Wiki`, real primary key - which
  inverts the failure mode from open to closed. The concrete class of leak this
  closes is a property computed from concealed fields: `effective_date_last_active`
  derives from two versioned fields, and the proxy answered it by delegating to
  the real row, so the fields were hidden and the conclusion drawn from them was
  not. `ProjectionTests` in `test_wiki_concealment.py` pins that case.
- **Substitution happens in `resolve_visible_wiki`**, the single gate all 99
  wiki-scoped call sites pass through - all 31 external API handlers among them,
  each of which calls `WikiApiView.resolve` as its first statement (verified by
  AST, not by reading).
- **Writes launder through `writable_wiki`.** The projection refuses `save()`,
  which turned nine write paths downstream of the read gate into latent 500s -
  and a 500 only gated accounts receive is the tell the feature exists to avoid.
  `bin/check_concealed_writes.py` is the eighth structural check and fails the
  build on a tenth.
- **`concealment_active`'s docstring** - which §5 called wrong in every
  particular, and which is the artefact whoever flips the boolean will read -
  has been rewritten against the current wiring.

Since then, in the same pass:

- **The Article tab.** An article is prose, so there is nothing to resolve
  field-by-field - but every `ArticleRevision` stores the *complete* source as
  of that revision, so a concealed viewer is shown the newest revision they may
  see, and no article at all when there is none. The rule with teeth is the null
  editor: it means a Wikipedia seed *or* a deleted account, and the model's own
  `editor_display_name` already distinguishes them, so `conceal_rows` does too.
  The revision history, the by-id diff and the by-id restore are all scoped to
  the same queryset - an unfiltered by-id lookup was an oracle that answered
  "does revision N exist here" and then handed over its diff.
- **Markup and detail pins** are hidden outright rather than filtered by author,
  per the product owner's ruling on map annotations. For detail pins that is
  also forced: a child wiki records no creator at all (see PROBLEMS.md).

Still open: the search and autocomplete substring oracles, which need a
provenance-carrying index rather than a patch, and `for_wiki(viewer=)` across
the remaining related-row querysets. Both are written up in `docs/PROBLEMS.md`
under their 2026-08-24 entries. §2's vacuous tests were fixed in `d08a74c2`.

---

## 1. Is the layer sound enough to keep building on?

The primitives are good and the reasoning in the comments is better than the code around them — `concealed_field_values`, `visible_actor_ids`, the fuzz-cache short-circuit, the "sole voter's own value *is* the composite" note at `wiki_stat_vote/queryset.py:85-90`, the alias `is_current` inversion caught at `controllers/aliases.py:119-123`. But the layer as built is a per-call-site branch on a predicate, repeated at ten sites, over a proxy that fails **open** by delegation. `docs/designs/concealed-wiki-spec.md` §4.2/§4.3 specified the opposite — resolve-time substitution and a viewer-aware `for_wiki` — and neither was built: `resolve_visible_wiki` (`services/wiki/wiki_access.py:391-402`) still hands back the raw `Wiki`, and every `for_wiki` in the tree still takes no viewer. The predictable result is that the four surfaces that were remembered are correct and roughly two dozen that were not are wide open, including the entire Article tab, ~30 of 32 external-API wiki handlers, and every write-path re-render on the page that the GET conceals. Keep the primitives; do not add a twelfth call site. And treat the test suite as unbuilt — six of its fourteen assertions do not exercise the code they name, which is why a self-review reported this clean.

---

## 2. Vacuous tests

The layer is dormant behind a stub. These tests are the only thing keeping it honest, and most of them are not.

**V1 — `test_the_photo_map_layer_omits_a_strangers_upload` (`tests/hypothesis/test_concealed_render.py:234-253`) — critical.** `image_gallery.py:383` applies `.visible_to(profile)` *before* `conceal_rows` at `:387-388`. `ImageQuerySet.visible_to` (`models/images/queryset.py:135-140`) admits an upload only via `_relationship_allows` against `photo_upload_visibility`, whose default `ANYTHING_IN_COMMON` (`models/profile/model.py:318-322`) needs a common pin, friend or trip. `ConcealedMediaTests.setUp:218-225` makes `self.stranger` a bare `baker.make(User).profile` with none of the three. The row is gone before concealment runs; deleting `image_gallery.py:387-388` leaves the test green. Fix: give the stranger a pin at the location, exactly as the comment test now does.

**V2 — the comment test had the same defect, and is fixed in the working tree but not committed.** `test_concealed_render.py:203` now adds `baker.make(Pin, profile=self.stranger, ...)`, with a docstring recording the diagnosis. `git diff --stat` shows the file dirty. Commit it — and note that the author found this defect in one test and did not check the sibling.

**V3 — `test_a_concealed_stat_composite_reads_as_never_voted_on` (`test_wiki_concealment.py:178-188`) — critical.** It calls `composite(self.wiki, WikiStatField.VULNERABILITY, viewer_conceals=True)` with no `viewer=`. `wiki_stat_vote/queryset.py:91` then short-circuits on `viewer is not None`, returns the empty composite, and the four votes cast at `:181-182` are never queried. The exact reasoning the branch's own comment calls "the subtle one" — the sole voter's value must come back as the composite — is what goes untested. `self.viewer` (`:155`) is created and unused. Both production call sites (`location_wiki.py:98`, `wiki_detail.py:110`) *do* pass `viewer=`, so the tested path is one production never takes. Fix: pass `viewer=self.viewer`, split into (a) no vote from the viewer → all-None, (b) after `cast(..., 2)` → `rounded == 2`.

**V4 — there is no positive control for rule 2 anywhere, at any level — critical.** `ConcealedRenderTests.setUp:43-67` records **no** AUTOMATIC revision: `baker.make(Wiki, ...)` goes through `VersionedModel.save` under `current_write_source()`, which is SYSTEM outside a request or task (`models/abstract/versioning.py:59-62`). `resolve_fields` matches `source IN ('automatic') OR actor_id IN (viewer)` (`versioned.py:375-378`) — neither the SYSTEM create rows nor the stranger's USER rows qualify. So `resolved` is empty for all 14 fields, everything falls to `field.get_default()` (`concealment.py:130-136`), and `test_the_page_still_renders_the_automatic_name:84-98` is asserting the *placeholder-name fallback* at `concealment.py:143-147`, not an automatic write. **Make `resolve_fields` return `{}` unconditionally and every test in the file still passes.** Fix: in setUp, `with writing_as(WriteSource.AUTOMATIC): Wiki.objects.filter(pk=...).update(description="Provider-supplied blurb")`, then assert that string is on the concealed page.

**V5 — `test_security_indicators_read_as_unset:100-107` asserts nothing about security indicators.** Its two assertions are `CANARIES["description"] not in body` and `"CANARY" not in body` — a strict subset of the flagship test at `:77-82`. The planted values are `SecurityLevel.SOME` (`:57-58`), rendered as the literal `Some` (`_wiki_about_card.html:38-45`), never asserted. It passes with `ALWAYS_UNSET` emptied — and would pass anyway, because per V4 those fields fall to `UNKNOWN` via the default regardless of the rule. Compounding: with concealment on, `description` is None, both dates unset, so the whole About card is suppressed at `_wiki_about_card.html:12` and the chip block at `:34` is unreachable. The `get_<field>_display` interception at `concealment.py:317-325` — written specifically for these chips — has **no test that reaches it**.

**V6 — `test_the_concealed_community_summary_never_reaches_the_fuzz_cache` (`test_wiki_concealment.py:157-176`) asserts a call that cannot happen.** `concealed_community_summary` (`concealment.py:188-215`) is a bare dict literal; the module imports nothing from `community_counts`. `fuzz.assert_not_called()` cannot fail. The property it claims to protect belongs to the caller ternary at `location_wiki.py:142` (and a second copy at `wiki_detail.py:185`), and deleting that ternary leaves this test green.

**V7 — the flagship test reads as four canaries and delivers two.** `CANARIES` (`:32-37`) is shared, but `pages/location/wiki.html` loads aliases by `hx-get` at `:189-194` and comments at `:363`; neither `CANARIES["alias"]` nor `CANARIES["comment"]` can appear in the page response. `ConcealedPanelTests`' docstring (`:162-167`) concedes this and the dict was left shared anyway. Split into `PAGE_CANARIES` / `PANEL_CANARIES`.

**V8 — rule 5 has no render-level test at all.** `grep -n 'Friendship\|friend' test_concealed_render.py` returns nothing. No test proves a friend's alias, link, comment, photo, edit or field value reaches any surface. Since every render assertion in the file is *negative*, a bug that concealed too much is invisible to the entire suite — and there is one live candidate: `aliases.py:115` and `links.py:71` re-derive the viewer as `getattr(request.user, "profile", None)` instead of using the profile `resolve_visible_wiki` returned two lines earlier; if that yields None, `visible_actor_ids` returns `set()` and the viewer silently loses their own and their friends' rows, green all the way.

**V9 — `test_the_page_carries_no_custom_layers:284-294` never asserts the response succeeded.** It inlines `self.client.get(...)` instead of using `_get` (which asserts 200 at `:231`). A 302 to login, a 404 or a 500 all pass.

**V10 — the module docstring (`:8-9`) says the tests drive "the real view and the real API payload".** Nothing in the tree calls `build_wiki_detail` with concealment on; grepping for `build_wiki_detail`/`viewer_conceals` finds only the composite call at `test_wiki_concealment.py:184`. Meanwhile `wiki_detail.py:137-187` is fully wired and `external_api/views_wiki.py:340` calls `concealment_active`. Either write the payload test or fix the docstring; right now it is a claim of coverage.

**V11 — the gallery positive control is satisfied by the wrong clause (`:255-282`).** The row is `profile=self.viewer_user.profile, source=WIKIMEDIA`. `conceal_rows`' Image branch is `~Q(source=UPLOAD) | Q(profile_id__in=allowed)` (`concealment.py:265`) and the viewer's own pk is always in `allowed` — both disjuncts true. Delete `~Q(source=UPLOAD)` and it still passes. Attribute the row to a stranger who shares a pin.

**V12 — `test_an_unknown_model_returns_nothing_rather_than_everything` (`test_wiki_concealment.py:339-350`)** never asserts `Trip.objects.all().count() == 1` first. True today (`TripManager` does no default filtering), one soft-delete flag away from asserting nothing. One line.

**V13 — `test_wiki_concealment.py:102-103`** asserts `assertNotEqual(values["cameras"], SecurityLevel.SOME)` — satisfied by `None`, `""` or `HEAVY`. The checkable value is exactly `SecurityLevel.UNKNOWN` (`models/abstract/security.py:25-32`), and it matters: `_wiki_about_card.html:34-45` renders a chip for anything that is not the literal string `"unknown"`, so `None` would pass this test and render eight chips. Iterate `ALWAYS_UNSET` and `assertEqual`.

**V14 — `ConcealedWiki` and `redact_edit_changes` have no unit tests.** Grepping both test files for `ConcealedWiki`, `conceal_wiki`, `redact_edit_changes`, `_display` returns nothing. Uncovered: the `save`/`delete` TypeErrors (`:331-337`), the display shim (`:317-325`), the non-dict guard and passthrough (`:375-382`). Four short tests.

The two history tests (`:128-158`) are sound — correct FK names, materialised before redaction, both canaries render unconcealed via `{{ diff.from }}`/`{{ diff.to }}` at `wiki_history.html:22-24`.

---

## 3. Still leaking

Ranked by what a concealed viewer learns. Everything here is confirmed by reading the file; `grep -c conceal` returns **0** for `article.py`, `albums.py`, `wiki_media.py`, `markup.py`, `boundary.py`, `detail_pins.py`, `property_owner.py`, `global_search/providers.py`.

**1. The entire Article tab.** `controllers/article.py` has no concealment at any layer. `ArticlePanelView.get:187-196` renders the full community body; `ArticleHistoryView.get:339-350` lists every `ArticleRevision` with `select_related("editor__user")`; `ArticleRevisionView.get:359-377` renders full diffs. Both panels are `hx-trigger="load"` (`wiki.html:335-336`, `:400-401`), so they fire on every page load, hidden tab or not. The API side is the same: `wiki_detail.py:178` calls `_article_summary(wiki, profile)` with no conceal branch, shipping `word_count`, `updated` and a masked-but-present `last_edited_by`. This is the largest single body of user-written prose on the wiki and `docs/designs/concealed-wiki-spec.md` §2.10 already specifies the rule (newest system-authored revision only, else the empty state). Nothing in this commit range touches it.

**2. The external API — ~30 of 32 wiki handlers.** `grep -n conceal external_api/views_wiki.py` returns exactly two hits, both in `WikiStatVoteApiView._payload:338-340`. Ungated, each verified: `WikiHistoryView.get:265-279` returns `"changes": edit.changes` **raw** — that is the `from` side `redact_edit_changes` exists to strip, for every editor, delivered by the same account that gets it redacted on the page; `WikiAliasesView:392-394`, `WikiLinksView:704-707`, `WikiGalleryView:782-799` (`visible_to` only), `WikiCommentsView:1118-1121` (`wiki.comments.all()`), `WikiArticleView:815-830` (full `content` and `content_html`), `WikiArticleRevisionsView:883`, `WikiArticleRevisionDetailView:928`, `WikiOwnershipView:671-674`, `WikiPropertySalesView:688-693`. `GET /wikis/{slug}/` reports `comment_count` off the concealed set (`wiki_detail.py:179`) while `GET /wikis/{slug}/comments/` returns the raw thread — a same-API contradiction. Commit `d4723e8f` concealed the HTML history and left the API twin, in the same range.

**3. Every stranger's `WikiLink` renders on the concealed page at first paint.** `wiki.html:118` includes `_wiki_about_card.html with wiki=wiki`, where context `wiki` is `shown` (`location_wiki.py:140`, `:196`). `links` is not a versioned field, so `ConcealedWiki.__getattr__` falls through to `return getattr(wiki, name)` (`concealment.py:326`) and hands back the real related manager. `_wiki_about_card.html:22` passes `links=wiki.links.all` into `_pin_links_row.html`, which renders `{{ link.url }}` and `{{ link.display_name }}`; `:12` gates the card's very existence on `wiki.links.exists`. `controllers/links.py:69-80` conceals the *identical* row correctly — but only on the add/delete/refresh round trip. There is no `hx-get` for `wiki-links-row` in `wiki.html`.

**4. The map loads all community markup and all child-wiki markers.** `location_wiki.py:190` and `:207` zero `custom_layers` and `map_overlays_json`, with a comment at `:186-189` calling this content "the single most direct statement that people go here" — and then `wiki.html:474/486/489` writes `data-markup-json-url` and `data-detail-pins-json-url` unconditionally, and `markup-toolbar.ts` / `map-annotations.ts` fetch both at map init. `MarkupJsonView.get` (`controllers/markup.py:187-225`) returns `PinMarkup.objects.for_wiki(wiki)` unfiltered; `LocationDetailPinJsonView.get` (`controllers/detail_pins.py:305-321`) returns every child wiki via `to_detail_json()` (name, description, coordinates, icon, colours). The containers are emptied while the contents load from unguarded endpoints. Same shape for the overlay dialog: `_map_annotations_panels.html:97-98` carries `hx-get="{{ manage_overlays_url }}" hx-trigger="load"` — page load, not dialog open — against `MapOverlayListView` with no viewer filter.

**5. Write-path re-renders return the panel the GET conceals.** Three of them, all same-page:
- `location_wiki.py:408` — `render_to_string("_wiki_about_card.html", {"wiki": wiki})`, the **live row**, swapped straight into the DOM by `wiki.html:555-557`. A concealed viewer who edits anything is handed back the real description, both dates, every community link and all eight security chips. The spec names this exact site (§1.4) and says it must return `""`.
- `comments.py:414` (post) and `:439` (delete) — `_build_context(wiki.comments.all(), ...)`, raw manager, while `:373-379` (get) conceals correctly.

**6. By-id write endpoints are unguarded, and one of them is worse than an oracle.** `LocationWikiRevertView.post` (`location_wiki.py:468-470`) resolves any `WikiEdit` by id scoped only to the wiki. A concealed viewer can revert an edit they were never shown; `revert_edit_fields` writes the stranger's pre-edit value onto the live row (`wiki_edits.py:238-240`, `:264`) under the reverting viewer's USER/actor binding, so `resolve_fields` then serves that value back to them as their own write — the leak `redact_edit_changes` exists to close, reached through the neighbouring endpoint. The toast at `:483`/`:490` also joins `skipped_fields`, naming fields touched by invisible edits. Same by-id pattern with no concealment filter: `LocationWikiEditDeleteView:510-512`, `LocationAliasDeleteView:254-270`, `LocationAliasUseView:272-292`, `LocationAliasToggleNicknameView:295-300`, `LocationLinkDeleteView` (`links.py:147-149`).

**7. Panels on the wiki page with no gate.** Albums (`albums.py:76-83`, `hx-trigger="load"` at `wiki.html:349-350`) — and photos reach the page through the album path without ever passing the gallery's `conceal_rows`. Media Photos tab (`wiki_media.py:132`, `hx-trigger="load"` at `wiki.html:286-287`) while the *Manage* pane of the same card conceals correctly — plus per-tile `MediaRelevance.vote_score` (`:96`, `:110`), which is community curation layered on provider media. Ownership and sale history (`property_owner.py:323-352`). Parcel buildings (`location_wiki.py:333`) — `building_rows` emits `child_name`/`child_uuid` per row, so the provider building list states that people have mapped this property. Device-scan markers (`external_api/views_device_scans.py:96-107`) — a marker exists only because someone walked the site scanning.

**8. Search and autocomplete return concealed content verbatim, without opening the wiki.** `WikiSearchProvider.search` (`services/global_search/providers.py:459-507`) emits `title=wiki.name` and `snippet=excerpt(wiki.description, ...)` from the live row and text-matches on `name`/`description`/`aliases__name`. `ArticleSearchProvider:512-549` full-text searches article content; `CommentSearchProvider:857-889` returns comment text plus display names; `autocomplete.py:96-98` substring-matches `location__wiki__description__icontains`. The substring match is the sharpest thing in this review: it confirms the *wording* of text the viewer has never been shown, from the map. `services/home/home_widgets.py:130` similarly renders the live `wiki.name` beside the viewer's own comments on their home page.

**9. Boundary geometry.** `WikiBoundaryView.get` (`controllers/boundary.py:233-239`) → `Boundary.objects.resolve_for_wiki`, which returns the wiki-scoped `drawn_or_generated_polygon` with `source="wiki"` whenever one exists (`queryset.py:256-257`) — and the only writers of wiki-scoped rows are user-driven. `wiki_detail.py:171` reaches the same geometry inside the one payload that is otherwise concealed.

**10. Provenance rules that classify wrongly.**
- **Image**: `concealment.py:265` keeps every non-UPLOAD row, on the premise (stated as fact at `:224-228`) that `Image.profile` on a provider row is the up-voter. That is false for at least two writers — the Flickr wiki-album import (`tasks.py:1928-1941`, `profile` = the importer) and `services/media/media_materialize.py:293-309`. And I found **no** writer that creates a wiki-scoped `Image` without a user action, so "provider rows stay: they are what a fresh wiki shows" describes rows a fresh wiki does not have. The spec §2.12 is titled "`Image.source` — looks like provenance, is not" and says never to classify on it.
- **WikiAlias**: `:275` keys on `source != "user"`. `models/aliases/signals.py:64-70` writes pin→wiki mirror aliases with `source="wiki_sync"` and a real `created_by`, all of which survive for every concealed viewer with the author sitting right there unread. In the other direction, `services/wiki/wiki_creation.py:186` seeds aliases at the model default `source="user"` with no `created_by`, so the wiki creator's own alias is dropped **for the creator** — a rule-5 violation on the same line.
- **NULL actor**: `:282` treats a null actor as automatic (`:222-223` states the premise). `WikiEdit.editor`, `WikiLink.created_by` and `WikiAlias.created_by` are all `SET_NULL` (`models/wiki_edit/model.py:41-47`, `models/links/model.py:99-105`, `models/aliases/model.py:134-140`) and `services/profile/account_deletion.py:194` really does delete the user. So every contribution by a deleted account becomes "automatic" and visible. Note the field-revision path answers the same question the opposite way — a deleted author's revision keeps `source="user"`, matches neither clause of `resolve_fields`, and drops out.
- **Dead and wrong entries**: `Floorplan`, `MarkupMap` and `WikiStatVote` (`:234-236`) have no `conceal_rows` caller at all — I enumerated every production call site. `MarkupMap: "profile_id"` actively encodes a friends-visible rule that contradicts rule 4; its `profile` is non-null CASCADE so the isnull half is dead and the entry does nothing but grant friends' maps. Rules 4 and 5 genuinely collide for `Comment.markup_map`: `conceal_rows` keeps a friend's comment and `_comment_body.html:48-50` renders its map.

---

## 4. New tells introduced by concealing

A concealed wiki must be in a state a real wiki could be in. These are states no real wiki is in.

**T1 — links appear, then disappear.** Per §3.3 the About card renders the full link set on first paint and the *concealed* set after any add/delete refresh (`links.py:143`, `:160`). Content vanishing from a page for one account is rule 6 defeated on its own.

**T2 — the About card exists at all.** `_wiki_about_card.html:12` gates on `wiki.description or wiki.date_abandoned or wiki.effective_date_last_active or wiki.links.exists`. `effective_date_last_active` is a plain property (`models/wiki/model.py:403-410`) reading `date_last_active`/`date_abandoned` — both versioned, both in `_values`, both concealed — but the property itself is not in `_values`, so `__getattr__` evaluates it **against the real row** (`concealment.py:326`). Result: the card renders "Last active \<real date\>" at `:28-29` beside a `date_abandoned` that renders as nothing, on a wiki whose every concealed field is empty.

**T3 — suggest-edits round-trips the concealed values over the live row and then names the differences.** The dialog is prefilled from `shown` (`wiki.html:421-436`, `location_wiki.py:242-251`) and posts every field (`wiki.html:534-535`). `apply_wiki_edit` diffs `str(raw) == str(old_val)` against the **live** row (`wiki_edits.py:126-127`), so concealed `""` vs real description, `unknown` vs a real `SecurityLevel`, `""` vs a real date all register as changes, get written, and are credited to the viewer. `location_wiki.py:409` then returns `list(changes.keys())` — precisely the set of fields where the hidden value differed. The `name` write also trips `Wiki.save()`'s alias auto-creation (`models/wiki/model.py:277-286`), planting an alias for the concealed name.

**T4 — the revert/alias endpoints are membership oracles.** Revert: 404 (no such edit) vs 400 "already reverted" vs 200-with-skipped-fields (naming the fields of an invisible edit). Alias add: `WikiAlias` has a case-insensitive unique constraint on `(Lower(name), wiki)` and `LocationAliasView.post:242-244` returns a distinguishable 409, so any candidate name can be tested against the concealed alias set. Alias delete compares to the live `wiki.name` (`aliases.py:257`) and returns a distinct 400 — an oracle for the hidden current name. "Use this name" bottoms out in the same live-row comparison and silently does nothing (`aliases.py:281-286`).

**T5 — the rename alias always disappears.** `Wiki.save()` auto-creates an alias with neither `source` nor `created_by` (`models/wiki/model.py:283-286`), so it defaults to `AliasSource.USER` and `conceal_rows:275` drops it for *everyone including its author*. Every real wiki with a meaningful name has exactly one such row; its absence is itself the difference. (Correction to an earlier claim: provider-sourced aliases from `persist_official_aliases_for_location` do survive and one can be marked current, so "no alias is ever current" is too strong.)

**T6 — the viewer's own history contradicts their own About card.** The eight security fields are in `WIKI_EDITABLE_FIELDS` and `apply_wiki_edit` records `{"from": …, "to": …}` (`wiki_edits.py:160`). `redact_edit_changes` deliberately keeps the `to` side (`concealment.py:379-381`), so history says "fences → some" beside a card reporting nothing known. Separately, `reverted` is untouched by the redaction, `wiki_history.html:11-15` renders the badge, and `conceal_rows` drops the stranger's revert row — so the UI states that an invisible person undid the viewer's edit, with no row explaining it. Rules 3 and 6 are in genuine conflict here and the code resolves it two different ways on one page; that needs a product decision, not a patch.

**T7 — the API payload contradicts itself in four places.** `"wiki_slug": wiki.slug` (`wiki_detail.py:158`) is a slugified copy of the live name, two lines above `"name": shown.name`. `"is_current": alias_is_current_name(alias, wiki)` (`:175`) is computed against the live name while the HTML twin was deliberately fixed to compare against `shown.name` in this very range — so a visible alias equal to the hidden name comes back `is_current: true` beside a different `name`, and vice versa. `"created"`/`"updated"` are full-precision ISO datetimes (`:180-181`) beside a `first_pinned` that `community_counts.py:106-118` deliberately truncates to the 1st of the month — and since `ensure_draft_wiki_for_location` creates the wiki from the `Pin` post_save hook, `created` *is* the first-pin moment. Every community edit bumps `updated` (`wiki_edits.py:76`), so `updated > created` states the row has been edited. And `link.order` is the stored order, so gaps over the filtered survivors count the removals.

**T8 — the scope badge re-exposes the concealed `pin_type`.** `location_wiki.py:216-217` passes the **live** wiki to `site_scope.is_site_scope` and `scope_badge`, in the same context dict that carefully reads `shown` for the eight security fields at `:242-251`. Both funnel into `effective_pin_type`, whose first branch is `if target.pin_type_is_user_provided: return target.pin_type` (`services/places/scope.py:155-157`) — the flag that means *a person chose this*, and which is not in `versioned_fields` (`models/wiki/model.py:174-189`). So the badge renders a user's choice while `shown.pin_type` says the default, and `WikiBuildingAttributesPanelView` (`:274`) 204s or renders a whole card on the same predicate. Passing `shown` is safe and gives the right answer.

**T9 — reactions on your own comment report an audience.** `_aggregate_reactions` (`comments.py:221`, `:226`, `:504`, `:519`, `:539-545`) groups every reaction with no actor filter, and `_comment_body.html:90` prints the count. Rule 5 guarantees your own comment survives; it then tells you six strangers reacted to it.

**T10 — two surfaces use own-only where the module promises own+friends.** `composite(viewer_conceals=True)` aggregates `filter(profile=viewer)` alone (`wiki_stat_vote/queryset.py:91`) and `boundary_vote_context(conceal=True)` does `others.none()` (`boundary_voting.py:291-292`). The spec §1.2 *does* specify own-only for both, and the reasoning is sound — a friend's ballot is indistinguishable from the community's inside an average — but neither site records that the narrower rule is deliberate, and the module docstring at `concealment.py:12-23` states the three-way union without caveat. That is the exact pattern this feature exists to stop.

**T11 — vote POSTs undo the guard.** `BoundaryVoteView.post` returns the true `has_consensus(location.place)` (`location_wiki.py:599`) eleven lines below the GET that passes `conceal=`, while `boundary_voting.py:305-313` documents at length why either answer is a tell. And because concealment sets `others = others.none()`, `auto_open` stays True — the concealed viewer is the one most likely to be handed this response. `PublicPinVoteView.post:567` omits `conceal=` the same way (reachable only by direct POST, since the block is not rendered).

---

## 5. False claims

**`concealment_active`'s docstring (`concealment.py:151-186`) is false in all three of its factual statements, and it is the operating instruction for whoever flips the switch.** It says four things are wired — the predicate is branched on at ten sites (`location_wiki.py:114`, `:424`, `:634`; `aliases.py:117`; `comments.py:377`; `links.py:73`; `image_gallery.py:354`, `:388`; `views_wiki.py:340`; `wiki_detail.py:139`). It says `concealed_field_values` and `conceal_rows` "have **no production callers**" — there are roughly a dozen. It says `build_wiki_detail` "does not consult this module at all" — `wiki_detail.py:137` imports four of its symbols. `git diff d27d82ac..fb8b5fa5 -- concealment.py` shows only `_ACTOR_FIELDS` and `redact_edit_changes` changed, so it was never revisited; parts of it were stale before this range began. It understates the wiring, which is the dangerous direction: it invites someone to flip the boolean expecting four surfaces to change.

**`conceal_rows`' Returns section (`:251-255`) states the opposite of what the code does.** "The narrowed queryset. **Unchanged** when the model is not in `_ACTOR_FIELDS` … see the KeyError path." The code does `_ACTOR_FIELDS.get(...)`, logs `"refusing to guess"` and returns `queryset.none()` (`:277-280`). There is no KeyError path — `.get()` cannot raise one — and "unchanged" contradicts the same sentence's own "cannot silently get an unfiltered result". The code is right and fails closed; the docstring is the only thing wrong, and for a security primitive that is the worse ordering. This is verbatim finding #16 of `docs/designs/concealment-review-2026-08-24.md:105`, still unfixed while `782dfdf9` fixed others from the same list.

**`ConcealedWiki`'s class docstring (`:294-296`) oversells both halves.** "Everything not overridden delegates … so a template or serializer can use this wherever it used a `Wiki` and pick up concealment without knowing it exists" — delegation is precisely the mechanism producing the links leak and the `effective_date_last_active` leak, and it will make every future `Wiki` property inherit the hole. "**Writes raise**" — only `save` and `delete` raise. There is no `__setattr__`, and `__getattr__` hands back writable related managers, so `shown.comments.create(...)` would write the real row silently and `shown.description = x` would shadow `_values` for the rest of the request. Spec §4.2 asks for "any manager write". No live write-through today; the read half of the same hole is already live.

**`location_wiki.py:137-139`** — "the concealed one cannot forget a field" — is scoped to `LocationWikiView.get`. The POST 270 lines later at `:408` forgets all of them.

**`location_wiki.py:186-190`** — markup "hidden outright, whatever their provenance" — suppresses two context keys while the markup itself loads from two unguarded endpoints.

**`_ACTOR_FIELDS`' header (`:224-228`)** states as settled fact that `Image.profile` on a provider row is the up-voter, not the photographer. False for the Flickr wiki import and for `media_materialize`.

**`test_concealed_render.py:8-9`** claims the tests drive "the real API payload". They do not. **`:84-98`**'s docstring describes asserting an automatic write; per V4 it asserts a placeholder fallback.

**Commit `d4723e8f` "conceal the edit history"** concealed the read path and left the API twin (`views_wiki.py:268`, raw `changes`) and three by-id write paths that address the same rows.

**Outside the module, three docs the code does not support.** `docs/TOOLING.md:112-124` says "Seven checkers … (CI)"; `ci.yml` runs five, `.pre-commit-config.yaml` runs a *different* five, `check_outage_not_cached.py` and `check_notification_choke_point.py` run in neither, and an eighth script (`bin/check_pin_not_published_to_wiki.py`) appears nowhere. `docs/designs/versioned-content.md:97-151` describes degraded-provenance production recording, a `sequence` column, `payload`/`parent`/`kind`, and an "automatic-HEAD pointer" — none of which exist (`sequence` was explicitly rejected as a read-modify-write race at `versioned.py:81-86`), and its claim that post-state-only recording "removes by construction" the `WikiEdit.changes` from-side leak is contradicted by `wiki_edits.py:167`. `models/reputation/signals.py:22-26` says `wiki_created` "now records at [its] transition instead", but the `Wiki` `_Subscription` is still installed with `created_only=False` (`:129-137`).

**And the checker added in this range cannot detect two of the three defects it was written for.** `bin/check_signal_reachable.py:79-82` records an *empty* field set for `connect`-style subscriptions, so `WikiEdit.reverted` (subscribed at `signals.py:233`, updated at `wiki_edits.py:287`) can never trigger `watched & written` at `:147`. `_updated_fields_by_model:99-108` resolves `self.filter(pk=...).update(officially_created=True)` (`models/wiki/queryset.py:238`) to a model named `"self"`. And the exemption parser at `:135` adds *every* whitespace-separated word after the marker, so the one live marker exempts `Pin`, `pin_created`, `is`, `create-only`, `on`, `purpose`, `A`, `pin` — repo-wide, with no file scoping — while the failure message at `:155` prints a format the docstring at `:30-32` contradicts. I ran it on the current tree: exit 0.

---

## 6. The one thing to fix first

**Make the resolver the chokepoint, and make `ConcealedWiki` fail loud instead of delegating.** Not any single leak — every confirmed leak in §3 is the same defect: a surface got the live row because it never called the predicate, or got the proxy and read straight through it. `resolve_visible_wiki` should return `(location, shown, profile, conceal)` with `shown` already concealed, so a handler cannot obtain the live row by default; and `ConcealedWiki.__getattr__` should refuse to delegate anything that resolves to a related manager or to a property reading a versioned field, rather than returning it. Both changes turn today's silent leaks into loud failures at the call sites that have them, which is how you find the ones this review missed. Spec §4.2/§4.3 already specify this; it was not built, and the twenty-odd unwired surfaces are the cost.

Riding along, because they are one line each and they are the difference between "dormant and honest" and "dormant and misleading": rewrite `concealment_active`'s inventory (`concealment.py:151-186`) to list the ten wired branch points and name the article, albums, media, ownership, markup, detail-pins, search and external-API list endpoints as **not covered**; fix `conceal_rows`' Returns section to say it fails closed; pass `shown` at `wiki_detail.py:175` and `location_wiki.py:216-217`; and wrap `location_wiki.py:408` in `conceal_wiki`.

Then fix the tests before the next surface. V4 is the one that matters — with no positive control for automatic content, `resolve_fields` could be deleted entirely and this suite would stay green, which means the suite cannot currently tell concealment from a blank page.