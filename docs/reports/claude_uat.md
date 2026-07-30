# UAT Notes — Round 2

**Tester**: Claude (AI)
**Date**: 2026-07-16
**Environment**: staging.urbanlens.org
**Account**: @manly_urbex (Jess Mann)
**Reference docs**: `docs/FEATURES.md`, `README.md`, `/dashboard/about/`

Round 1 findings are in `docs/reports/ua_testing.md`. This document covers the second pass after reported changes, with a focus on feature completeness and verification of every section listed in FEATURES.md.

---

## Overall Assessment

The app is substantially more polished than Round 1. The pin detail page title issue (raw coordinates in the `<title>`) has been fixed. The filter sidebar, pin list, enrichment panels, and all major nav pages are working. The most critical remaining issue is the raw Google API error still rendering in the Street View panel, and the REST API being broken on staging.

---

## Map & Pins

### Filter Sidebar

Tested all filter sections. All work and correctly update the map:

- **Scores** — Expanded to show 4 range sliders: Rating, Danger, Priority, Vulnerability. All render correctly. Sliders interactive.
- **Visits** — Radio chips "Visited" / "Never visited". Selecting "Visited" filtered the map from 46 markers down to ~5 green checkmark markers in under a second. URL updated with `has_visits=yes`. ✅
- **Date Pinned** — After/Before date pickers. Functional.
- **Labels** — Searchable chip list showing all user labels. Clicking a label chip filters pins. ✅
- **Overlapping pins only** — Checkbox toggle present. ✅
- **Saved Filters** — "Test" saved filter visible with toggle and delete buttons. ✅

**Issue**: The filter panel has a "FILTER (1)" indicator when a saved filter is active, but the counter is not obvious — looks like part of the close button area. Consider making it more prominent.

### Pin List Panel

- Opens from the `_togglePinListPanel()` button on the right edge of the map. ✅
- Shows "53 places to view" with a paginated list (Page 1 of 5, ~10 per page). ✅
- Each row shows pin icon, name, status/category label chips. ✅
- Clicking a pin row zooms the map to that pin's location. ✅
- "Add these pins to a list" button at top opens a modal to select or create a trip/list. ✅

**Issue**: Clicking the pin list row navigates the map but does NOT open the pin detail. To view details, you must click the marker on the map after it zooms. Adding a "View details →" link to each row would improve discoverability.

**Issue**: Several pins in the list display raw coordinate strings as their name (e.g., `39.118881, -84.529554`). This is the same root issue as Round 1 M1.

### Add Pins to a List (Trip integration)

The "Add these pins to a list" button in the pin list opens a modal ("Add to a List") showing:
- Search box for lists
- "+ Create a new list" option
- Existing lists: "Prioritize", "Test List" (with item count)

This is a nice feature for batch-adding filtered pins to a trip or collection. Not listed as a distinct feature in FEATURES.md (worth adding — see FEATURES.md update at the end of this document).

### Layer Switcher

Tested all layers via the Layers panel. Working layers:
- Street (OpenStreetMap) ✅
- Terrain ✅
- Satellite (Esri) ✅
- Weather ✅
- Pins (overlay toggle) ✅
- Child Pins (overlay toggle) ✅
- Dark ✅
- Borders ✅
- Places ✅

**Issue (from Round 1, unresolved)**: Basemap options (Street/Terrain/Satellite/Dark) and overlay toggles (Pins/Child pins/Weather/Borders/Places) are mixed in the same list without visual grouping. A user could accidentally "stack" two basemaps. Consider separating with a divider and using radio vs. checkbox behavior.

**Note**: FEATURES.md mentions "topographic" but UI shows "Terrain" — these are the same layer.

### Bulk Select Mode

The "Select pins" button activates a mode (confirmed via `aria-expanded="true"` on the button after clicking), but there is no visible map-level feedback (no outline, no banner, no changed cursor). This was a Round 1 finding (M10) and remains unaddressed.

### Import Pins

"Import pins" button visible in the map toolbar. Not fully tested in this pass (would require file upload), but the button is present and accessible. ✅

---

## Pin Detail Page

Tested on: `118 W 9th St` and `Carew Tower`.

### Fixed since Round 1

- **Page title**: h1 now correctly shows "118 W 9th St" / "Carew Tower". ✅
- **Browser tab title**: Now shows "UrbanLens" (generic but acceptable). Previously showed raw coordinates.

### Working panels

- **Details section**: Star ratings (Danger, Priority, Rating, Vulnerability), address, coordinates. ✅
- **"Create Community Wiki" button**: Visible in top right for pins without a wiki. ✅
- **Links section**: Shows "No links yet." with option to add. ✅
- **Location map**: Full-width Leaflet map with pin marker. Building boundary polygon visible for Carew Tower (salmon/pink filled polygon). ✅
- **Satellite Imagery**: Esri World Imagery carousel with "Current / High Resolution" label and carousel dots. ✅
- **Photos**: "No photos yet — click to upload, or drag files here." Clean empty state. ✅
- **Visit History**: "No visits recorded yet." Clean empty state. ✅
- **Organization**: Shows applied labels as chips (e.g., "Abandoned", "Permission Spots"). ✅
- **Aliases**: "No aliases yet." Empty state. ✅
- **Custom Fields**: Descriptive empty state explaining what custom fields are for. ✅
- **Ownership**: "Owners | Sale History" sub-tabs. "No owner information yet." ✅
- **Media**: Combined panel with sub-tabs: All (31), Google Maps (10), Smithsonian (5), Wikimedia (16). Smithsonian and Wikimedia images load correctly. ✅
- **Web Results**: 10 results from web search. Results load. ✅
- **OpenStreetMap (Nominatim)**: Returns neighborhood, suburb, county, postcode. "View on OpenStreetMap →" link. ✅
- **Photon (OpenStreetMap)**: Secondary OSM result showing nearest named feature ("The Church on Ninth"), full address breakdown. ✅
- **Regional Data**: Sub-tabs for US Census, Wildlife, Seismic, EPA. Loads data on-demand per sub-tab. ✅
- **Comments**: Text input with camera/attachment icons and "Post" button. ✅

For landmark pins (Carew Tower), additional panels appear:
- **Wikipedia**: Article content loads with section headers (History, Planning and construction, etc.). ✅
- **News**: News search results for the location. ✅
- **Building Characteristics**: Structured property data. ✅

### Remaining issues

**CRITICAL (unresolved from Round 1)** — Street View panel still shows raw Google API error text:
```
Google Maps Platform rejected your request. This IP, site or mobile application is not authorized to use this API key. Request received from IP address 163.102.88.211, with referer: https://staging.urbanlens.org/
```
The error is in a black text box in the panel. This must never be shown to a user. Either fix the API key authorization for the staging server's IP, or catch this error and display "Street View is not available" instead.

**Issue** — Google Maps media (10 items in the "All" tab) appears as broken gray placeholders in the Media panel. Same API key authorization issue as Street View.

**Issue** — Web Results includes wrong-city matches. For "118 W 9th St", results included addresses in Los Angeles, CA and Tracy, CA. The search is not constrained by the pin's city/state (Cincinnati, OH).

**Issue** — The loading bar that appears at the top of the pin detail page on initial load is jarring. It's a full-width gray bar with "Loading…" text that disappears once panels render. Per-panel loading spinners are sufficient; this global bar is unnecessary.

**Not observed** — The following panels from FEATURES.md were not seen on any tested pin:
- Library of Congress (may be in the "All" media tab but no separate UI label was visible)
- USGS Historical Topo Maps (may only appear for US locations with historical survey data)
- LoopNet commercial real-estate listing (not seen as a panel; LoopNet appeared as a Web Results link)
- NPS panel (not seen; may only appear for pins near national parks)
- OpenWeatherMap weather panel (not seen on pin detail; weather appears on Trip detail instead)

These panels may only appear conditionally based on data availability, which is reasonable behavior. Worth noting for documentation.

---

## Trips

### Overview Page (`/dashboard/trips/`)

- **Header**: "Trips — Plan a trip with your friends, or just yourself." ✅
- **Tabs**: Overview, Events, Calendar. ✅
- **Stats**: Total trips (3), Upcoming (1), In progress (0), Planning (1), Completed (1). ✅
- **Calendar**: July 2026 mini-calendar with trip events shown on their dates ("abc test" on Jul 15–16). ✅
- **Recently updated / Recently viewed** lists. ✅
- **"+ New Trip" button**. ✅

### Trip Detail Page (`/dashboard/trips/neopex/`)

- **Header**: Trip name ("Neopex"), date range ("Aug 7, 2026 – Aug 9, 2026"). ✅
- **Tabs**: Overview, Events, Calendar. ✅
- **Trip map**: Embedded Leaflet map. Shows "Add activities to see them on the map." when no activities are geo-located. ✅
- **Activities section**: Sub-tabs: Upcoming, Proposed, Confirmed, Completed. One activity ("Campground — Confirmed"). ✅
- **Members panel**: 2 members listed — "Alyssa @_knitten_" and "Jess Mann @manly_urbex (CREATOR, RSVP: YES)". ✅
- **Comments section**: Text input with image/attachment buttons, "Post" button. ✅
- **Weather section**: Shows trip dates. Displays "No location data" because the activity has no coordinates.
- **Onboarding tooltip**: Appeared on first visit — "Members can RSVP and organizers can manage planning permissions." with "View members" / "Later" / "Don't show again" options. ✅

**Issue**: Trip map uses a plain OpenStreetMap tile layer (no satellite option). Inconsistent with the main map's default satellite view.

**Issue**: Weather section shows "No location data" without explaining how to fix it. Should say something like "Add a location to an activity to see the weather forecast."

**Issue**: The "Campground" activity has no per-activity voting controls (thumbs up/down) visible, despite FEATURES.md listing "per-activity thumbs up/down voting on proposed activities". This may be because the activity is already "Confirmed" (not "Proposed"), so voting is no longer applicable — but this isn't communicated.

**Not tested**: Google Calendar sync (requires connecting a Google account).

---

## Safety

### Overview Page (`/dashboard/safety/`)

- **Header**: "Safety — Let people know when to expect you back, and who to alert if you don't check in." ✅
- **Tabs**: Overview, Settings. ✅
- **"+ New Check-in" button**. ✅
- **Explanation card**: Clear "HOW IT WORKS" description of the feature. ✅
- **Stats**: 0 Total check-ins, 0 Active, 0 Checked in safely, 0 Contacts notified. ✅
- **Empty state**: "No check-ins yet — Create a check-in before your next trip so someone knows to look for you." with shield icon. ✅

No check-ins exist to test the full flow. Feature presence confirmed.

---

## Memories

### Timeline View (`/dashboard/memories/`)

- **Header**: "Memories — Everywhere you've been - routes, trips, visits, and photos, on one map and timeline." ✅
- **Tabs**: Timeline, Photos, Maps, Sharing, Journal, Visits (60). ✅
- **"Import routes & history" button**. ✅
- **Stats** (Last 90 days): 1915.9 mi distance, 7 places visited, 0 photos, 3 trips, 5 weeks active. ✅
- **Date range filter**: From/To date pickers with presets (Last 90 days / Last year / All time). ✅
- **Map**: Shows routes, visits, and trips as colored markers across the US. Legend: Routes (blue), Trips (orange), Visits (green), Photos (red). ✅
- **Timeline**: Grouped by month (July 2026, June 2026). Each entry shows place name, entry type (Visit/Journal, Trip), and source. ✅

**Issue**: One timeline entry shows raw DMS coordinates (`39°07'31.5"N 84°35'51.3"W`) as the place name — consistent with the unnamed-pin issue throughout the app.

**Issue**: The map on Memories uses OpenStreetMap (no satellite option). Inconsistent with main map.

**Not tested**: Photos tab, Maps tab, Sharing tab, Journal tab (no data in these sections for this account's 90-day window).

---

## Organize (Labels)

### Labels Page (`/dashboard/organize/`)

- **Header**: "Organize — Manage the labels used to organize your data." ✅
- **Top-level tabs**: Labels, Lists, Filters. ✅
- **Label type sub-tabs**: Tags, Categories, Statuses, People, Media, Display Order. ✅
- **Labels list**: Each entry shows icon, name, optional description, count badges, child label indicators, and color dots. ✅
- **View options**: Grid/compact/table view toggles, sort button, filter button, + add button. ✅
- **Label examples**: "Prioritize", "TODO", "Doesn't Look Interesting" (with description), "Notable", "Additional Dangers" (with description), "Alyssa", "Alyssa's A List". ✅
- **Child labels**: "Alyssa's A List" shows "Alyssa" as its parent. ✅

**Not tested**: Creating/editing/deleting labels, merging labels, bulk operations. Feature presence confirmed.

---

## Profile

### Profile Page (`/dashboard/profile/`)

- **Header**: Avatar, display name (Jess Mann), username (@manly_urbex), bio, email, social links. ✅
- **"View as" button**: Present and functional (opens profile as another user would see it). ✅
- **"Edit Profile" button**: Present. ✅
- **Profile / Edit tabs**. ✅
- **About section**: Bio text. ✅
- **Social links**: Mastodon (@manly_urbex), Discord (ManlyUrbes). ✅
- **"My Private Activity" section** (yellow, "Only visible to you" label): 
  - Stats: 1 Maps created, 3 Wiki edits, 3 Trips created, 2 Pins rated. ✅
  - Recently viewed pins list. ✅
  - Recently created pins list. ✅
  - High-priority places to visit. ✅
  - Recently viewed wikis. ✅

**Issue (Critical, unresolved)**: Email address (`jess.a.mann@gmail.com`) still displayed in the profile header and visible to all logged-in users. "Contact Visibility" setting defaults to "Friends Only" but the email still appears publicly. This is unchanged from Round 1.

**Issue**: "Recently viewed pins" list includes raw coordinate pin names (same root issue as M1).

---

## Settings

### Privacy Tab

- **Profile Visibility**: Anyone (Logged In) ✅
- **Comment Visibility**: Anyone (Logged In) ✅
- **Friend Requests**: Anyone (Logged In) ✅
- **Photo Visibility**: Anyone (Logged In) ✅
- **Show Photos From**: Anyone (Logged In) ✅
- **Trip Pins**: Anyone (Logged In) ✅
- **Contact Visibility**: Friends Only ✅

All dropdowns are functional. The Privacy section is comprehensive.

**Issue**: Despite "Contact Visibility" being set to "Friends Only", the email address still appears on the public profile page (see Profile section above). The setting doesn't appear to be enforced.

### Other Tabs (not fully tested)

- **Connections**: Likely Google Calendar and Discord OAuth. Not tested.
- **Messages**: Not tested.
- **Security**: Likely password change, 2FA. Not tested.
- **Theme**: Not tested.
- **Account**: Likely account deletion, email management. Not tested.
- **Advanced**: Contains Undo History.

### Undo History (Settings → Advanced → Undo History)

Direct URL `/dashboard/settings/undo-history/` renders as a bare, unstyled HTMX fragment — no app shell, no navigation, white background. Content present ("No recent deletions. Deleted pins, wikis, safety check-ins, and trips show up here for 7 days.") but completely unstyled.

**Issue**: This URL should either render with the full page shell or redirect to the settings page with the correct tab/section open.

---

## About Page (`/dashboard/about/`)

- **Hero**: "URBAN EXPLORATION PLATFORM / Built for explorers who respect what they find." with app screenshot showing a pin detail (Nevele Grand Hotel) with building boundary polygon in pink. ✅
- **"Open my map" CTA button**. ✅
- **"Your personal atlas of forgotten places." section** with copy explaining the product. ✅
- Further sections not fully scrolled (marketing content).

**Minor**: For logged-in users, the hero CTA ("Open my map") could say "Go to your map" since they already have a map.

---

## REST API

- `/dashboard/rest/` → **500 Internal Server Error** ❌
- `/dashboard/rest/pin/` → **404 Not Found** ❌
- `/dashboard/rest/pins/` → **404 Not Found** ❌

The DRF API is entirely broken on staging. The map loads pins through a separate mechanism (likely the HTMX-driven map view endpoint), so the map itself works, but the documented REST API surface (`pins`, `reviews`, `profiles`, notifications) is unreachable. This would break any API client or integration.

---

## Notifications

Notification bell is present in the top navigation. Not tested (no new notifications during this session). WebSocket connection for real-time push was not specifically verified.

---

## Feature Verification Against FEATURES.md

| Feature | Status | Notes |
|---------|--------|-------|
| Interactive Leaflet map — satellite/street/topographic layers | ✅ | 9 layers including terrain, dark, weather |
| Pin CRUD (add/edit/delete) | ✅ Partial | Add and detail confirmed; edit/delete not tested |
| Add pin by map click / coordinate / place search | ✅ UI present | Buttons confirmed |
| Pin list view alongside map | ✅ | 53 pins, paginated |
| Bulk pin operations | ⚠️ Partial | Select mode activates silently; no visible feedback |
| Per-pin aliases | ✅ | "No aliases yet" on detail page |
| Import/export (GPX, KML, etc.) | ✅ UI present | Import button confirmed, not tested end-to-end |
| Community Wiki | ✅ | "Create Community Wiki" button on pin detail |
| Wikipedia panel | ✅ | Present on Carew Tower |
| Smithsonian / Wikimedia Commons | ✅ | In Media panel |
| Street View | ❌ | Raw API error displayed |
| Satellite imagery carousel | ✅ | Esri World Imagery working |
| Web/image search panels | ✅ | Web Results (10 results) working |
| OSM / Nominatim | ✅ | Both Nominatim and Photon panels working |
| Regional Data (Census, Wildlife, Seismic, EPA) | ✅ | On-demand per sub-tab |
| OpenWeatherMap | ⚠️ | Seen on Trip detail, not pin detail |
| NPS panel | ❓ | Not seen — may be location-dependent |
| LoopNet | ❓ | Not as a panel; appeared as a Web Results link |
| USGS Topo Maps | ❓ | Not seen — may be location-dependent |
| Library of Congress | ❓ | Not seen as explicit panel |
| Building boundary polygons | ✅ | Visible on Carew Tower |
| Photo galleries (upload, lightbox) | ✅ UI | Upload prompt present; no photos to test lightbox |
| Memories timeline | ✅ | Working with month grouping |
| Memories photos tab | ❓ | No photos to verify |
| Trips (multi-stop, RSVP, voting, comments) | ✅ Partial | RSVP and comments working; voting UI not seen |
| Trip calendar view | ✅ | Calendar on trips overview |
| Google Calendar sync | ❓ | Not tested |
| Safety check-ins | ✅ UI | No check-ins to test flow |
| Friendships / social layer | ✅ | Members on trip show friends |
| Labels / Tags / Categories / Statuses | ✅ | Full Organize page working |
| Notifications (bell) | ✅ UI | Bell visible; not tested |
| Settings (privacy, connections, etc.) | ✅ | 7 privacy settings confirmed |
| Undo history | ⚠️ | Functional but bare when accessed at direct URL |
| REST API | ❌ | 500/404 on staging |
| About page | ✅ | Marketing page present and polished |

---

## New Features Observed (Not in FEATURES.md)

1. **"Add pins to a list" bulk action** from the pin list panel — opens a modal to select a trip or collection and add all currently-visible/filtered pins to it at once. Should be documented under Mapping & Pins.

2. **Regional Data panel on pin detail** — US Census, Wildlife, Seismic, EPA sub-tabs loading regional data on demand. More specific than the existing OSM/Nominatim listing.

3. **Photon (OpenStreetMap) panel** — A second, separate OSM-sourced panel distinct from the Nominatim panel. Shows the nearest named OSM feature and full address breakdown.

4. **Building Characteristics panel** — Structured property data (seen on Carew Tower). Different from Ownership.

5. **News panel** — Web news results (distinct from the general Web Results panel, seen on Carew Tower).

6. **Journal tab in Memories** — Visit entries logged via device journal (distinct from manually marked "visited" pins).

7. **Sharing tab in Memories** — Presence confirmed in tab bar.

8. **"Import routes & history" on Memories page** — Separate from the pin import flow on the map. This imports GPS tracks / location history.

9. **Saved Filters** — Users can save a named filter configuration for quick reuse. Visible as toggle buttons in the filter sidebar.

10. **Onboarding tooltips on trips** — Contextual in-product help that appears on first visit to a trip, explaining member permissions. "Don't show again" option present.

---

## Priority Issues for Next Fix Cycle

1. **Street View raw error (CRITICAL)** — Display a friendly error instead of the raw Google API rejection text.
2. **Email on public profile (CRITICAL)** — The Contact Visibility setting is not being enforced; email always shows.
3. **REST API broken (MAJOR)** — `/dashboard/rest/` returns 500; all documented endpoints return 404.
4. **Unnamed pin fallback (MAJOR)** — Pins without a name display raw coordinate strings throughout the UI (pin list, profile, memories timeline). Use a consistent, user-friendly fallback.
5. **Undo History page shell missing (MAJOR)** — Direct URL `/dashboard/settings/undo-history/` renders as unstyled HTMX fragment.
6. **Web Results geo-constraint (MAJOR)** — Search results include wrong-city matches; append city/state to query.
7. **Bulk select visual feedback (MODERATE)** — Selecting pins mode gives no visible indication it's active.
8. **Trip map basemap (MODERATE)** — Use satellite or provide layer-switcher on trip detail map.
9. **Weather "No location data" (MODERATE)** — Add explanatory text about how to fix this.
10. **Layer switcher grouping (MODERATE)** — Separate basemaps from overlays visually and behaviorally.
