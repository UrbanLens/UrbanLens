# UrbanLens UAT — Round 3
**Date:** 2026-07-17  
**Tester:** Claude (automated walkthrough)  
**Focus:** Features skipped or only lightly touched in Rounds 1–2: messaging, bulk operations, import, pin detail tabs, settings sub-pages, safety check-in creation, profile/friends.

---

## Summary

| Area | Status | Notes |
|---|---|---|
| Bulk pin select | ✅ Working | Toolbar with Add to List / Merge / Edit / Delete appears correctly |
| Import Pins dialog | ✅ Working | All formats listed; AI document import present |
| Notifications panel | ✅ Working | AI tasks, friend requests, messages all show |
| Direct Messaging | ✅ Working | E2E encrypted DMs and group chats functional |
| Pin detail — Article tab | ✅ Working | Private per-pin article with revision history |
| Pin detail — Comments tab | ✅ Working | Compose input with image/mention icons |
| Pin detail — Edit History tab | ✅ Working | Article revision history (empty state clear) |
| Settings — Privacy | ✅ Working | 9 controls, 7-option granularity each, name-source priority drag list |
| Settings — Connections | ✅ Working | Immich, Google Photos, Google Calendar (connected), keyword tagging |
| Settings — Messages | ✅ Working | Disappearing messages, E2E recovery key, online/read/typing indicators |
| Settings — Security | ✅ Working | Passkeys, TOTP authenticator, backup codes |
| Settings — AI | ✅ Working | Master on/off + per-type auto-label controls; AI link analysis |
| Settings — Account | ✅ Working | 500 GB storage, 11 notification events × 4 delivery channels |
| Settings — Advanced | ✅ Working | Custom fields for pins, photos, people, maps |
| Safety check-in creation | ✅ Working | Form complete; MarkupMap, grace period slider, default message |
| Messages nav dropdown | ⚠️ Bug | Shows "all caught up" even when active conversations exist |
| Group chat URL navigation | ⚠️ Bug | Direct URL to group chat removes thread from sidebar, doesn't load |
| Safety map default zoom | ⚠️ Minor | Opens at whole-US zoom instead of user's area |
| Email on public profile | ❌ Ongoing | Still exposed in profile header (critical — Round 1/2 finding) |
| Raw coordinates in UI | ❌ Ongoing | Still appearing in recently-viewed pins, recently-created pins (Round 1/2 finding) |

---

## Direct Messaging (`/dashboard/messages/`)

This is a fully built feature that was not tested in previous rounds and is not documented in FEATURES.md.

**What's there:**
- Full-page two-pane messaging UI: conversation list left, thread right
- Direct messages (1:1) and group chats (`/g/<uuid>/`)
- Rich compose toolbar: image attachment, share location/map, share pin (`folder_shared`), @mention, emoji
- Read receipts (`done_all` icon) and per-message reaction button
- End-to-end encryption active by default. "Encryption keys changed" notices appear inline when keys rotate. Old messages encrypted under a rotated key display as "Unable to decrypt on this device" in a muted style.
- Encryption managed in Settings → Messages: view/reset recovery key, set disappearing-message expiry (never / on read / 1 day / 30 days / 90 days / 1 year)

**Settings → Messages tab:**
- Show Online Status: 7-option visibility
- Show Read Receipts: 7-option visibility
- Show Typing Indicator: 7-option visibility
- Delete My Messages After: 6 expiry options (disappearing messages)
- Allow Friend Recommendations toggle
- Encryption: recovery key view/reset

**Bugs:**

### Major — Messages nav dropdown always shows "all caught up"
The bell-adjacent Messages button in the global nav opens a small dropdown saying "You're all caught up / New messages will show up here" even when active conversations with recent messages exist at `/dashboard/messages/`. The dropdown appears to fetch no conversations at all. Users would not know to visit the full page.

### Major — Group chat URL navigation removes thread from sidebar
Navigating directly to `/dashboard/messages/g/<uuid>/` causes the group conversation to disappear from the left sidebar and the right panel remains at the "Select a conversation" empty state. Clicking away and re-entering via the conversation list is the only working path. Deep linking to group chats is broken.

### Minor — "Unable to decrypt on this device" UX
When messages can't be decrypted (key rotation after device change), the text "Unable to decrypt on this device" with a lock icon is shown in the thread at the message position. This is technically correct but visually alarming — red/muted styling suggests an error. A softer treatment (e.g. grey italic "Message from another device") would be less confusing. An option to import the old key would help users who still have it.

---

## Bulk Pin Operations (Map)

Clicking the "Select pins" toolbar button (checkbox icon) enters multi-select mode and displays a bottom bar with:

- **n selected** count
- **Clear** button
- **Add to List** — bulk-add visible/filtered pins to a trip or saved list
- **Merge** — merge multiple pins into one
- **Edit** — bulk-edit shared fields
- **Delete** — bulk-delete with undo
- **×** — exit select mode

All action buttons remain disabled until at least one pin is selected. The mode works correctly — the bulk toolbar appeared and buttons labeled as expected.

---

## Import Pins Dialog (Map)

Clicking "Import pins" opens a modal supporting:

- KML, KMZ, GeoJSON, GPX, OSM XML, WKT, WKB, CSV, ZIP/TGZ archives
- Shapefiles (must be zipped)
- Google Takeout HTML export (My Activity/Maps)
- Plain text `.txt` and Word `.docx` (AI document import)
- Photos (auto-matched to pins by GPS + timestamp)

The **AI document import** feature is highlighted in a callout: upload any unstructured document with location mentions and AI identifies them, with a review step before import commits.

A "How do I get a Google Takeout file?" accordion is present.

---

## Pin Detail — Article, Comments, Edit History Tabs

The pin detail page has four top-level tabs: **Overview**, **Article**, **Comments**, **Edit History**.

### Article tab
Private per-pin long-form notes, formatted like a Wikipedia article with sections, links, and references. Completely private to the logged-in user. Empty state: "You haven't written an article for this pin yet" with a "Write your article" CTA — clear and well-worded.

### Comments tab
Compose box with user avatar, image attachment icon, @mention icon, and blue "Post" button. Comments on pins with no prior comments show only the compose box — there is no empty state message (e.g. "Be the first to comment"). This is a minor polish gap.

### Edit History tab (Article Revisions)
Lists every saved version of the pin's private article. Empty state: "No article revisions yet. Write your article from the Article tab and every saved version will appear here." Clear and helpful.

### Note on tab labeling
The "Edit History" tab label in the nav corresponds to a section titled "Article Revisions" inside. The mismatch between the tab name and section heading is a minor inconsistency — they should match.

---

## Notifications

The notifications dropdown correctly shows:
- AI background task completions ("AI link analysis complete — AI finished reading a link for Point 134: N field(s) updated")
- Friend request events with resolved status shown inline ("You accepted this request." with a check_circle icon)
- New message notifications (with encrypted message previews showing 🔒)

Notification settings (Account tab) cover 11 event types × 4 delivery channels (in-app, email, WhatsApp, SMS). WhatsApp and SMS require a phone number to be added to the profile before they become active.

---

## Settings Deep Dive

### Privacy tab
Nine visibility controls, each with seven options: Anyone (Logged In) / Users with anything in common / Users with a pin in common / Users with a friend in common / Users with a trip in common / Friends Only / No one.

Controls: Profile Visibility, Comment Visibility, Friend Requests, Photo Visibility, Show Photos From, Trip Pins, Contact Visibility, Direct Messages, Pins in Common.

Additional:
- **Community Features** toggle (wikis, trips, friend requests)
- **Show Wiki Cover Photos** toggle
- **History toggles**: Visit History, GPS Routes, Live Location, Photo Keywords
- **External Services** toggle
- **Name source priority**: draggable reorder list with 8 geocoding sources (Wikipedia, Google Places, NPS, Photon, OpenStreetMap, EPA ECHO, Azure Maps, OpenStreetMap former name)

### Connections tab
- **Immich** (self-hosted photo server): Server URL + API key fields, "Connect Immich" button
- **Google Photos**: OAuth connect for importing photos from library
- **Google Calendar**: Connected (jess.a.mann@gmail.com), "Disconnect" button shown
- **Browser Permissions**: Location (enabled) and Notifications (enabled) status displayed, with instructions for disabling via browser site settings
- **Keyword Tagging**: Free local keyword matching (no AI required); master toggle + per-type sub-toggles (auto-categorize, auto-tag, auto-status)

### Security tab
- Set a password for OAuth accounts (enables new-device encryption unlock without recovery key)
- Passkeys (Face ID, Windows Hello, security keys, Bitwarden-compatible)
- TOTP authenticator app (Google Authenticator, Authy, Bitwarden TOTP)
- Backup codes (require passkey or TOTP to be set up first)
- 2FA is off by default; adding a passkey or TOTP turns it on

### AI tab
- Master "Enable AI Features" toggle (disables everything at once)
- Per-type auto-labeling: auto-categorize, auto-tag, auto-status on pin creation
- AI link analysis: reads external links on pin detail pages and fills in fields; results shown in notifications

### Account tab
- Storage: 500 GB quota; photo downscale preference (1920px default / 1280px / 800px)
- Notification preferences per event type and delivery channel (WhatsApp/SMS deferred until phone number added)
- Undo History (inline, not a separate page): "Restore pins, wikis, safety check-ins, and trips you deleted in the last 7 days" — 0 recent deletions currently; "Clear undo history" button
- Danger Zone: account deletion with 7-day soft-delete grace period

### Advanced tab (Custom Fields)
Define private custom fields for pins, photos, people, and maps. Currently 0 fields configured for all four entity types. Each type has an "Add field" button. This is a power-user feature for tracking non-standard attributes.

---

## Safety — New Check-in Creation (`/dashboard/safety/new/`)

Form fields:
- **Title** (optional) — placeholder "e.g. Solo Hike - Eagle Ridge"
- **Expected Check-in Time** (required) — defaults to ~2 hours from now
- **Grace Period** — slider control; shows calculated alert time in real time ("Contacts will be notified at …")
- **Trip Plan** — free text for route, gear, companions
- **Destination & Route** — embedded MarkupMap with drawing tools (line, arrow, text, box, circle, polygon tools)
- **Message to Emergency Contacts** — pre-filled with a sensible default message explaining the check-in purpose
- **Emergency Contacts** — shows existing friend contacts (_knitten_, Sarah, John); selectable

The form is well-designed. One issue:

### Minor — Safety check-in map defaults to full-US zoom
The embedded MarkupMap in the check-in creation form opens showing the entire United States at a very high zoom level (zoom ~3). Users would need to navigate significantly to reach their actual location. It should default to the user's current GPS location, last known map center, or home location.

---

## Profile Page

**Confirmed from previous rounds (still present):**
- Email `jess.a.mann@gmail.com` still visible in the public profile header
- Raw GPS coordinates (`39°08'19.1"N 84°31'55.8"...`) still appearing in "Recently viewed pins"
- Recently created pins show "tmp (deleteme)" and "test deleteme" — test data, not a bug

**New observation:**
- "My Private Activity" section uses a clear gold/amber border and "Only visible to you" badge — effective UX for communicating private vs. public content
- "Recently viewed wikis" shows "Dropped pin" as a wiki title, which occurs when a dropped-coordinate pin has had a community wiki created for it — the wiki name should fall back to the location's address rather than "Dropped pin"

---

## Features Added to FEATURES.md This Session

The following features observed during this UAT were not previously in FEATURES.md and have been added:

1. **Direct Messaging** — E2E encrypted 1:1 and group DMs with disappearing messages, read receipts, typing indicators, and an encryption recovery key
2. **Private Pin Article** — per-pin Wikipedia-style private article with full revision history
3. **Immich integration** — connect a self-hosted Immich photo server to browse and import nearby photos
4. **Keyword Tagging** — local (non-AI) keyword matching for auto-categorizing, auto-tagging, and auto-statusing pins
5. **Custom Fields** — user-defined private fields for pins, photos, people, and maps
6. **Passkeys + TOTP 2FA** — optional two-factor authentication via passkeys, authenticator apps, or backup codes
7. **Disappearing messages** — configurable message expiry per-account (on read through 1 year)
8. **Safety check-in MarkupMap** — draw a route/destination on a map when creating a safety check-in
9. **Azure Maps** — geocoding source included in the user-configurable name-source priority list
10. **Photo blur by visibility** — "Show Photos From" setting: photos from users outside your chosen visibility level are blurred rather than hidden

---

## Priority Issues

1. **(Ongoing Critical)** Email address visible on public profile — privacy risk pre-launch
2. **(Ongoing Major)** Raw GPS coordinates used as display names throughout the UI (profile, visits, recently viewed)
3. **(New Major)** Messages nav dropdown always empty; active conversations not surfaced — users likely miss the feature
4. **(New Major)** Group chat URLs don't work; navigating to a group by URL breaks the sidebar
5. **(Ongoing Major)** Delete and View Details buttons appear in the Add Pin creation dialog before the pin exists
6. **(Minor)** Safety check-in map opens at full-US zoom — needs to center on user's location
7. **(Minor)** Comments tab has no empty state message when there are no comments
8. **(Minor)** "Edit History" tab name mismatches the "Article Revisions" section heading inside it
9. **(Minor)** "Unable to decrypt on this device" message styling is alarm-red; should be softer/more informative
