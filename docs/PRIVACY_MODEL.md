# What is public, what is private

**Status: DRAFT — written to be corrected.** This document states what the code
*currently enforces* and what I believe the *intent* is. Where those differ, it says so.
Jess: correct the **Ruling** columns and answer the numbered questions at the end. Once
corrected, this becomes the checklist for sweeping the project so the rules are codified
and enforced everywhere.

Verified against `@release/v_0_7_0` on 2026-08-24.

---

## 1. The core rule

Everything a user creates is **private by default**. It becomes visible to another user
only when **both** of these hold:

1. **The container gate.** The owner deliberately shared the item into a *container*
   (a wiki, a trip, a DM, ...) and the viewer can reach that container.
2. **The settings gate.** The owner's visibility settings greenlight *that viewer*.

These are conjunctive. Access to the container is not enough; permissive settings are
not enough. Both, every time.

**The one exception: direct messages.** Sending someone a photo *is* the act of consent,
so the settings gate is skipped for the recipient of a DM. (A separate per-pair
`DirectMessageImagePermission` handshake still applies — the recipient must accept images
from that sender.)

### What this rule rejects

A user with a pin at the same location as me is **not** thereby entitled to see the
photos I attached to *my* pin there. Having a pin in common is a fact about the world;
it is not consent. My photos stay private until I put them on the wiki myself.

---

## 2. The containers

`Image` has seven container foreign keys. Each is a distinct answer to "who can reach
this?", and the container gate has to enumerate all seven:

| Container | Who can reach it | Settings gate applies? |
|---|---|---|
| `pin` | the owner only — a pin is a personal record, not a container | n/a — never shared by being on a pin |
| `wiki` | anyone with wiki access (§3) | **yes** |
| `location` | not a share by itself | n/a |
| `trip` (via activities) | trip members | **yes** |
| `direct_message` | the recipient | **no — sending is consent** |
| `safety_checkin` | anyone who can see the check-in, **including signed-out token contacts** | **no** |
| `visit` | owner only, as far as verified | n/a |
| `pin_suggestion` | the suggestion's owner | n/a |

> **Known gap.** `ImageQuerySet.visible_to` currently treats `wiki__isnull=False` as the
> whole of "shared". That is simultaneously **too narrow** (a photo shared to a trip or a
> check-in reads as unshared) and **too broad** (it never asks whether the viewer can reach
> *that particular* wiki). See §6.

---

## 3. Wiki access

Wiki access is **not** "has a pin there". It is a place-domain rule with four clauses
(`services/wiki/wiki_access.py`) — a viewer reaches a wiki if any hold:

1. they have a pin on the wiki's exact `Location`;
2. they have a pin whose place shares the wiki's `Place.domain_root_id` (the parcel, or
   any building on it);
3. the domain is reachable through `MEMBER_OF` aggregate places, resolved by a fixpoint
   capped at `MAX_EARNING_ROUNDS = 16`;
4. a `PlaceAccessGrant` row grants it.

This is deliberate: **users must discover a location before they can see its wiki.**

A wiki the caller has not earned access to must return **404, never 403** — a 403 confirms
the wiki exists, which leaks the location.

---

## 4. The settings gate

`VisibilityChoice`, least → most restrictive: `ANYONE` (logged in) · `ANYTHING_IN_COMMON`
· `COMMON_PIN` · `COMMON_FRIEND` · `COMMON_TRIP` · `FRIENDS` · `NO_ONE`.
`ANYTHING_IN_COMMON` = a common pin **or** a common friend **or** a common trip. Accepted
friends qualify for every option except `NO_ONE`.

| Setting | Default | Governs |
|---|---|---|
| `profile_visibility` | ANYTHING_IN_COMMON | who sees the profile |
| `comment_visibility` | ANYTHING_IN_COMMON | who sees your comments |
| `friend_request_visibility` | ANYONE | who may send you a friend request |
| `photo_upload_visibility` | ANYTHING_IN_COMMON | **who sees photos you contribute to locations** |
| `viewer_photo_filter` | ANYTHING_IN_COMMON | whose photos *you* want to see (outside → blurred) |
| `trip_pin_location_visibility` | ANYTHING_IN_COMMON | who sees the real coordinates of a pin you add as a trip activity (others see only the name) |
| `contact_visibility` | FRIENDS | phone, Signal, Discord, WhatsApp, Telegram, Matrix |
| `direct_message_visibility` | ANYTHING_IN_COMMON | who may DM you |
| `online_status_visibility` | FRIENDS | online indicator in DMs |
| `read_receipt_visibility` | FRIENDS | read receipts |
| `typing_indicator_visibility` | FRIENDS | typing indicator |
| `common_pins_visibility` | FRIENDS | which specific pins you share with a viewer (**both** sides must allow) |

`viewer_photo_filter` is **courtesy-only**. It is set by the *receiving* user about what they
want shown to them, so it never grants sight of anything and never gates anyone else's access —
its job is to stop unsolicited explicit images.

`photo_upload_visibility` is a limit on **who, among people who can already reach the
container, may see the photo**. It is *not* permission to publish a pin photo to a wiki.
Nothing in the product currently asks for that permission except putting the photo on the
wiki yourself.

When `community_enabled` is False, the community-gated settings are forced to `NO_ONE`
and the wiki-sync booleans to False.

All contact fields are stored in `EncryptedTextField`.

---

## 5. Pin data: what a share carries

Sharing a pin shares **the coordinates and a small set of objective facts about the
site** — not your record of it. Rulings below are Jess's.

**Shared** — objective, not personal:
`location` · `pin_type`, `pin_type_is_user_provided` · `indoor_outdoor` ·
`date_built`, `date_abandoned`, `date_last_active` ·
`fences`, `alarms`, `cameras`, `security`, `signs`, `vps`, `plywood`, `locked`

**Name** — only with explicit consent. The sharer may supply `shared_name`; otherwise the
recipient's pin is named from an **official** `WikiAlias` (`source != USER`), never from
the sharer's own label.

**Never shared:**

| Field | Why |
|---|---|
| `description` | the owner's personal notes; nothing in the product lets somebody consent to passing them on |
| `vulnerability`, `danger` | personal ratings about the site, not facts about it |
| `icon`, `color`, `custom_icon`, `priority` | personal styling and triage |
| labels / detail styling | personal organisation |
| photos | opt-in only, per §1 |
| `parent_pin` | the sharer's own tree. Sharing a pin that has a parent should **ask** the sharer how to handle it; the sharer's parent must never be passed through to the recipient |

**Photos, when a share does carry them:** keep `taken_at`/dates, `latitude`/`longitude`,
`direction`. Drop `caption` and `exif_data`. `author` becomes the existing author, or
`"Shared by {username}"` when unset.

---

## 6. Where the code diverges today — the worklist

1. **`ImageQuerySet.visible_to` implements one container, not seven** (§2), and never
   checks reachability of the specific wiki. This is the largest open gap.
2. **Five inline reimplementations of wiki access** that never call `wiki_access.py`, so
   they cannot inherit fixes: `models/article/queryset.py:18`,
   `services/global_search/providers.py:372,463,509,851`,
   `services/map_pins/autocomplete.py:138`, `services/consensus/eligibility.py:37`.
3. **Consensus photo rounds** do not apply `visible_to`; fixing needs a `build_round`
   protocol change.
4. **`exif_data` is a plain `JSONField`.** Intent: encrypt it at rest and strip it from the
   image file. *Stripping done 2026-08-24* — the block now comes off every stored file
   unconditionally, with `exif_transpose` applied first so nothing renders rotated. **Encryption
   at rest is still outstanding** (it needs a field change and a migration).
5. **`ocr_text` is a plain `TextField`** — derived text from a possibly-private photo, and
   a plausible search-leak path. Not yet audited.
6. **Existing profiles** predate the stricter photo default and still need migrating.
7. **Nit:** `pin_sharing.py:217` sets `name_is_user_provided=True` when the name actually
   came from an official alias rather than from the sharer.

### Not a divergence — designed, not yet built

The hidden reputation gate that scales how much wiki detail a new account sees —
defeating "pin a random address and check whether a wiki exists" probing — is **designed
in full and not implemented**. See `docs/designs/reputation-and-gating.md` (494 lines,
written 2026-08-21 from the 2026-08-20 voice memo; tickets UL-397/UL-398/UL-399). Its
status line: *DRAFT — needs Jess's input on the open questions in "Decisions needed"
before any code lands.* Decisions 1–2 are resolved; **3–6 are open and block the build**:
coefficient tunability, the v1 list of "sensitive", whether "earn your way in locally"
ships in v1, and anti-gaming scope.

Nothing in the codebase implements it today, so **no read path currently consults any
reputation signal.** Four things use adjacent vocabulary and are not it:
`wiki_access.py`'s "earned access" (the place-domain rule, binary);
`ConsensusProfile.trust_score` (a Beta posterior that weights *fact evidence at
submission* — all 28 references are confined to consensus/facts, none on a read path);
`ProfileTrust` (a private 1–5 star rating one viewer keeps about another); and wiki
`vulnerability` (a consensus-*voted* field, which the design does intend to use as the
gate's sizing input).

---

## 7. Rulings (Jess, 2026-08-24)

Answers to the questions this document was written to ask. Each names the work it implies.

1. **Trips.** A pin photo attached to a trip activity **is** shared to a container for all trip
   members — **and** it must still pass the owner's visibility settings before any particular
   member sees it. Both gates, as everywhere except DMs and check-ins.
   → *Done 2026-08-24:* trip clause added to the settings-gated term, as a subquery on activity
   pin ids (a join would repeat a row per activity).

2. **Check-ins.** Photos on a safety check-in are **not** subject to `photo_upload_visibility`,
   because it makes no sense for safety contacts who have no account at all. They are visible to
   **anyone who can see the check-in, including signed-out token holders**. Reaching the check-in
   is the only barrier.
   → *Done 2026-08-24:* the portal now lists the check-in's photos, and
   `safety.contact.photo` serves their bytes to a valid magic-link token, scoped to that token's
   own check-in. The nginx hand-off was extracted from `MediaGateView` rather than reimplemented.

3. **`viewer_photo_filter`.** Courtesy-only, as read. It is controlled by the *receiving* user, so
   it has nothing to do with gating that user's access — it exists to stop unsolicited explicit
   images. It never grants sight of anything.

4. **Derived data.** `ocr_text`, `checksum`, and `redata_confidence` **inherit the privacy of the
   photo they came from.**
   → *Verified 2026-08-24, nothing to fix:* `checksum` is only ever used for owner-scoped dedup
   (`profile=profile`, or within one check-in) and is never serialized; `ocr_text` is a
   `PhotoSearchProvider` search field whose results pass through `visible_to`;
   `redata_confidence` is a sort key on querysets that already apply `visible_to`.

5. **Location.** The model is a coordinate plus official data about that coordinate, existing so
   other things can link to it rather than restating it. It **must not be queryable on its own** —
   not through the REST API, not through search, not any other way, because that would let a user
   fuzz which coordinates are attached to things. Serving coordinates *alongside* a model the
   caller may see is fine.
   → *Verified 2026-08-24:* structurally satisfied. The DRF router registers only `PinViewSet`,
   there is no Location search provider, and `/locations/search/` is autocomplete over the
   caller's **own pins** plus external places, not a Location query. Nothing to fix; worth a
   structural check so it stays that way.

6. **Deletion.** For a photo **the user uploaded**, deleting it somewhere other than the wiki
   **prompts**: "delete from the wiki too?" Silence means no — it stays. For **external photos**
   (fetched from a URL) there is **no prompt at all**; they stay on the wiki unless the user goes
   to the wiki and deletes it there, because a public resource that already exists online is not
   a consent question.
   → *Work:* neither the prompt nor the external/uploaded distinction exists yet.

7. **Earned credit.** **Not now** — finish the work above first. The design in
   `docs/designs/reputation-and-gating.md` stays parked.
