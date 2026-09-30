# N31 — Decisions agents made without Jess's input, awaiting her review

`id: N31` · `status: current` · `updated: 2026-09-30`

A read-only audit of the 269 commits since the N29 audit (`6d0b7d956`) and of N30's dispositions, made after
Jess found two agent changes that overrode her direction (reverted as D24 and D25). It lists changes that remove,
cap, throttle or refuse something users could do before, or that apply a third party's policy or a policy
value, with no record of Jess approving them. Git author "Jess Mann" on agent commits is not evidence of approval.
Approved and left out: the enumeration rulings (P147/P149), D20, and the two "per Jess" pin commits.

Each row gets a ruling from Jess; until then, nothing here is reverted.

## Data deleted or requests refused

| Commit | What changed for users | Agent's premise |
| --- | --- | --- |
| 97128e703, migration 0096 | **Ruled 2026-09-29:** a pasted image is always downloaded and served locally as soon as it's provided (it already was at submit); 0096 now refuses rather than deletes. Still open: import drops foreign tile templates | No network in a migration; N29 G3-1/G3-11/G6-22 privacy |
| e7d83e2ae, migration 0098 | **Ruled 2026-09-29:** a value with no scheme is read as `https://` (stored rows repaired, not deleted); a real TLD is required; `mailto:` is refused. Still open: custom-field text truncated at 5,000 chars, now a write cap | A crafted file stored `javascript:` links (G4-26) |
| 72f6f96a1 | Per-account caps: 100 saved filters, 500 lists, 2,000 labels, 100 custom fields, 10 push devices, 5,000 photos per album; imports skip over-cap rows, undo-restore refused | "One account set the cost of every page" |
| e3b54f91e | Group chats capped at `max_group_chat_members` (default 20, was a hardcoded 50); 100 group chats per user; inbox pages at 50. EXTERNAL_API.md still says 1–50 members | Bound the inbox query |
| 63faab024 (G3-31) | SSO sign-up whose verified address belongs to an existing account is refused (no linking by email); an unverified provider address isn't the account's email until confirmed; migration 0067 cleared stale/duplicate verified-email proofs | Account takeover via SSO. Touches D24: review alongside it |
| 1a824ea72 | A second live subscription to a role already held is cancelled at Stripe, first payment not refunded automatically; checkout refuses a held role | Duplicate subscriptions from two tabs |
| 925b11c68 | Nightly purge of read notifications after 365 days (still open). **Ruled 2026-09-29:** device scans are never deleted; the device-scan purge is gone, and an account's scans outlive it (ac27ca4d7's cascade reverted) | Unbounded tables (G4-11/12/13) |
| 0a7a1ef72 (D21) | Uploads serialised per user; one waiting over 20 s gets a 429 | Quota race (G3-21); D21 says "not yet confirmed by Jess" |
| eb37afb43 | External API `/memories/timeline/` moved from `page`/`page_size` to `before`/`limit` (max 100); `count`/`previous` always null | Unbounded query (G2-32) |
| 356c2ca6a, 178cc8939 (P148) | Parcels/sites over 10 km² and buildings over 1 km² can't resolve, so they drop out of wiki and pin-in-common domains; large real sites can exceed this | A county-sized hull |

## Medium

| Commit | What changed | Premise |
| --- | --- | --- |
| a689b6638, migration 0092 | Trip activity times limited to 1900–2199; stored times outside nulled | Bad dates broke rendering |
| fd75dc12e | Per-queue task time limits; import preview's networked half stops at 170 s, rest reported "unfinished" | Tasks inherited a 1-hour limit (G3-8) |
| 11634341e, d53dbce4e, fa89cf500, 038082329, a149d5e72 | Per-account throttles: place autocomplete 120/min, nearby/place details 60/min, historical maps 60/min, trip weather 30/min, Flickr albums 20/min, social-link probe 20/10 min, geolocation ping 30/10 min, Immich thumbnails; upstream-backed routes refuse while the counter store is down | Protect paid/shared upstream budgets |
| c68eb5dc7 | Settings address geocode requires login and is limited to 20 upstream lookups an hour | "Drain the app-wide Nominatim budget", the D25 premise |
| 628dbcb46 | Source-document viewer serves only PDFs and raster images, judged from the bytes | HTML from REData was served on our origin (security; recommend keep) |
| 540c39890 | API-key reads logged to the usage trail at most once a minute per key | Write per call |
| 89ae9d8ca | Server-side password policy skipped for E2EE-derived credentials; a modified client can set a weak password | Derived credentials can't be checked server-side |
| 8199de5e9 | A password change deletes all OAuth2 tokens (signs out the native app) and closes sockets; API keys revoked only if ticked | G3-35/36 |
| aa4b83003 | Unset `UL_ENVIRONMENT` means production; deployments refuse to start without required config; CORS/CSRF stop trusting `urbanlens.org` and `localhost` everywhere | Unsafe defaults (G3-6) |

## Low

e9c86ca6d (paid-API limiter refuses when its count query fails; five LLM features obey admin limits),
08a39e4ea (push never retried on a network error), f7e5b4afe (5-minute resend cooldown and budget),
0f469eea8 (avatars capped at 512 KB; Wayback redirects only to archive.org), paging in ed2739f06/a35b3c7a8/e3b54f91e
(trips 24 per page, calendar a month at a time, visit suggestions 5 per page), 729461c22 (floorplan features show
the ground floor unless `?level=`; a save drops people/media labels), cefca1813 (parcel split probes at most 20
neighbours), e03b780eb (concurrent wiki edits get a 409), ac27ca4d7 (device-scan trails deleted with the account).

Just before the audit window, same kind: d25e8b168 (news limited to the site's language and the place's country;
mostly non-Latin headlines dropped), bd959d6d1 (a mail guard drops Gmail recipients with characters Gmail never
issues), 5c82d2d6f (a nested pin adopts its site's panel answers only within 1 km).

## N30 "Kept" items

Each is an agent's disposition. The two security ones and the unverified one come first:

- **G2-1 residual (security):** the login-params endpoint is unthrottled. Its decoy answers make it "not an
  oracle", and a per-address cap "would bite shared NATs".
- **Batch 7 (security):** achievement icons are open to any signed-in account, including secret achievements'.
- **Batch 15 (security):** Gotify alerts bypass the public-URL guard, because the URL is admin-set on a LAN.
- **Batch 29 (unverified):** the channel-layer buffer is unchanged. Whether every client catches up after a
  dropped frame was never checked.
- **G3-10 / G5-32:** floorplan and map pages load every label, list and field, bounded by the caps above.
- **G2-14:** trip forecast on the request, up to 8 s. **G2-12:** profile preview creates rows in a rolled-back
  transaction. **G1-31:** settings geocode inline.
- **Performance and structure:** single pin save refits its parent in the signal; parent delete cascades,
  with a whole-subtree undo; the PWYW ledger sweep is one task; one-to-one `get_or_create` without retry; external
  API page offsets uncapped; trivia generation sequential; safety overview unpaginated, and a null auto-delete
  window means "never"; file sizes (G1-30).

## Questions raised 2026-09-29, after the rulings

- **Overview and Property Records.** `14783279d` moved Historic Registers and Building Characteristics to
  Property Records. Since then the Overview neither names a National Register listing nor prefetches those
  tabs (`location_data_overview` skips uncached PROPERTY sources), so an empty Property tab isn't hidden until
  something else fetches it. `HistoricRegisterPanelSource.overview_summary` and the Overture one are
  unreached, the Playwright spec `hrsh-panel-layout.spec.ts` still expects the Overview mention, and
  `test_location_data_overview.py::test_nothing_ready_schedules_every_source_and_returns_pending` fails.
  Restore the Overview mention and prefetch, or retire them with their tests?
- **P49.** Dated records (designs, archive, audits) hold 92 citations past the end of today's files, so CI's
  citation check is red. Exempt dated directories from the check, or rewrite those citations?
- **P165.** Provider thumbnails (media gallery, web search, historical sheets, satellite slides) load
  browser-direct from provider hosts. Proxying them hides viewers' IPs but moves that traffic onto our
  servers, the trade-off D25 decided the other way for geocoding. Proxy, allowlist, or leave as is?
- **Imported tile templates.** Import drops a tile URL template that isn't this site's own route (97128e703).
  Tiles can't be downloaded once like an image. Keep dropping, or import and serve them browser-direct?
- **Account deletion and device scans.** Scans now outlive their uploader's account with `profile` cleared,
  per the "never deleted" ruling; the readings are still a timestamped route, unattributed.
- **KML areas.** A Polygon or MultiGeometry placemark used to fail the whole KML import; it is now a pin at
  its centroid, matching the GeoJSON importer (1683a8bce). Say if areas should be skipped instead.
- **Links that name a user.** A value with no scheme like `paypal.com@evil.ru` is refused now, since reading
  it as `https://` would store a link that shows one host and opens another. The same link typed with its
  scheme (`https://paypal.com@evil.ru`) is accepted, as it always was. Refuse user-naming links outright, or
  show the real host when rendering them?
- **Migration 0096 stops rather than deletes.** Production had no URL-only overlays on 2026-09-29, so it
  passes there. An environment holding one stops at 0096 until `manage.py download_overlay_image_urls`
  stores its image; the command deletes nothing and names any overlay it has to leave.
- **Admin "Top Locations".** The site-admin stats table has always been empty: it checks for an
  `annotate_pin_count` queryset method that never existed (found by P85's typing; `TODO(P85)` in
  `controllers/site_admin.py`). Fill it, or remove the table?
- **Community albums and overlays (P29).** Any viewer of a wiki can delete another contributor's community
  album or map overlay: both routes check visibility, not authorship. Restrict deletion to the author (and
  moderators)?
- **Smaller P29 surprises.** `dev_toolbar.*` answers an anonymous caller 403 rather than a login redirect;
  `trivia.kick` refuses a non-host with 400 rather than 403; `label.bulk_convert*` reads `{"ids": "12"}` as
  ids 1 and 2. Fix any of these, or leave them?

## Rulings 2026-09-30

Jess answered a numbered list of every open decision:

- **P165 thumbnails:** download and cache locally forever, with provenance.
- **Community albums and overlays (P29):** leave deletion open. Wikis are community resources; personal things
  belong on the private pin page.
- **P168 device scans:** scans are separate records that never overwrite anything; display is a summary of all of them. Done (P168 archived).
- **Login-params throttle (G2-1):** a generous per-address limit (30 a minute), done 2026-09-30. P177 records the raw-password fallback it exposed.
- **Achievement icons (batch 7):** whichever is simpler and faster. **Gotify LAN URL (batch 15):** fine as is.
- **Links that name a user:** refuse outright (done).
- **I7 blocks:** 1b and 2b (see I7). Done (I7 absorbed).
- **P170 revisions:** keep all, store diffs, and leave alone until the earlier context (concealed users) is found.
- **Account deletion and device scans:** fully anonymise the scans; timestamps and routes may stay (done: any profile deletion clears the uploader and the client session token).
- **P3:** remove the picker (done). **P172:** child pins listed, merged with building lists, see P172.
- **`label.index`:** it duplicates the Organize tabs, so delete it (done). **Dev toolbar dark mode:** add a button (done).
- **Admin Top Locations:** remove the table (done).
- **Overview and Property Records:** hers to delegate: consolidate so nothing is duplicated and no empty tab shows. Done 2026-09-30: the
  National Register sentence leads the Historic Registers tab (`national_register_note`), the Overview no longer
  carries Property Records content (both unreachable `overview_summary` methods removed), and the Overview schedules
  uncached Property Records sources so an empty tab is found and hidden.
- **Imported tile templates:** she challenged "tiles cannot be downloaded once"; rebuild a recognised map sheet onto
  this site's route, and proxy-and-keep any other host per the P165 ruling.
- **KML areas:** keep the centroid pin. **Migration 0096:** write the download command.
- **P167:** deferred. **P49:** the citation check runs manually, as a warning; strip line numbers from the flagged citations.
- **The caps and throttles tables above:** no objection raised when offered "keep unless named".
