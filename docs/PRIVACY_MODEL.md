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
| `safety_checkin` | the check-in's contacts | **yes** |
| `visit` | follows the visit's own visibility | **yes** |
| `pin_suggestion` | the suggestion's recipient | **yes** |

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
4. **`exif_data` is a plain `JSONField`.** Intent: encrypt it at rest and **strip it from
   the image file**. Currently neither happens.
5. **`ocr_text` is a plain `TextField`** — derived text from a possibly-private photo, and
   a plausible search-leak path. Not yet audited.
6. **Existing profiles** predate the stricter photo default and still need migrating.
7. **Nit:** `pin_sharing.py:217` sets `name_is_user_provided=True` when the name actually
   came from an official alias rather than from the sharer.

### Not a divergence — a missing feature

Jess described a gate where users without enough **earned credit** cannot see content on
**vulnerable** wikis. **This does not exist on this branch.** Four things use adjacent
vocabulary and are not it: `wiki_access.py`'s "earned access" (the place-domain rule,
binary); `ConsensusProfile.trust_score` (a Beta posterior that weights *fact evidence at
submission*, never a read); `ProfileTrust` (a private 1–5 star rating one viewer keeps
about another); and wiki `vulnerability` (a consensus-*voted* field). If the gate is
required, it is new work.

---

## 7. Questions

1. **Trips.** A pin photo attached to a trip activity — visible to trip members subject to
   `photo_upload_visibility`, or does joining a trip imply broader consent?
2. **Check-ins and visits.** Do photos on a safety check-in follow `photo_upload_visibility`,
   or should the check-in's contact list override it (safety beating privacy)?
3. **`viewer_photo_filter` blurs.** Is blurring meant as a *courtesy* filter over photos the
   viewer is already entitled to, or does it ever *grant* sight of something? (I read it as
   courtesy-only.)
4. **Derived data.** Do `ocr_text`, `checksum`, and `redata_confidence` inherit the privacy
   of the photo they came from? Checksums in particular allow cross-user existence probes.
5. **Location vs Pin.** `Location` is shared, canonical data. Is any field on it ever
   private, or is the whole model public-by-construction once a user can see the location?
6. **Deletion.** When a user deletes a photo that has been contributed to a wiki, does the
   wiki keep it (community contribution) or lose it (owner's right to withdraw)?
7. **Earned credit.** Should §6's missing gate be built now, and against what signal?
