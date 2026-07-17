# UA Testing Report — staging.urbanlens.org

**Date:** 2026-07-16  
**Tester:** Claude (automated walkthrough)  
**Scope:** Full site navigation — Map, Pin Detail, Organize, Trips, Memories, Safety, Profile, Search, Notifications

Issues are grouped by severity: **Critical** (broken or misleading), **Major** (clearly wrong UX expectation), **Minor** (polish/improvement).

---

## Map (`/dashboard/map/`)

### Major — Map tiles fail to render on initial load
On first page load, the Leaflet map renders with a broken tile grid (gray blocks instead of satellite imagery) that fills only the center of the viewport. Tiles load correctly after any interaction (click, pan, zoom). This is likely a Leaflet container initialization race condition where the map's pixel dimensions aren't settled before tile fetching begins. A `map.invalidateSize()` call after the DOM stabilizes should fix it.

### Minor — Pin popup requires a precise click
Clicking on approximate pin locations on the map doesn't always open the popup. The click target is very small. Increasing the marker hit area would improve usability.

---

## Pin Detail (`/dashboard/map/pin/<coords>/`)

### Critical — Page title is raw GPS coordinates
The `<h1>` heading displays `39°08'19.1"N 84°31'59.8"W` instead of the pin or location name. This is the first thing a user sees and is completely opaque. The location name (`3016 US-127`) and official name (`39°08'19.1"N…`) are available in the Details section below — the location's canonical name should be used as the heading, falling back to coordinates only when no name exists.

### Critical — Raw Google Maps API error displayed in Street View panel
The Street View section shows a raw error string directly to users:
> "Google Maps Platform rejected your request. This IP, site or mobile application is not authorized to use this API key. Request received from IP address 163.182.80.211, with referer: https://staging.urbanlens.org/"

This exposes internal infrastructure details and looks broken. The panel should show a user-friendly fallback (e.g. "Street View unavailable for this location") when the API call fails.

### Critical — Google Maps photo thumbnails all broken
The Photos from Google Maps section shows six placeholder/broken image icons. No images load. This may be the same API key misconfiguration as Street View, but the visual result is a grid of broken boxes that looks like a rendering failure.

### Major — News, Building Characteristics, and LoopNet Listings panels stuck loading indefinitely
Three panels on the pin detail page show a "Loading…" spinner that never resolves. There is no timeout, no error state, and no empty state fallback. Users have no way to know whether to wait or whether something is broken.

### Major — "What are aliases and nicknames?" popover fires on every page load
The explanatory popover for the Aliases section appears on every visit to a pin detail page, blocking the Aliases content. If this is a first-run tooltip it should only appear once (gated by a localStorage or user preference flag); if it's always-on documentation, it should be collapsed by default, not an overlay.

### Minor — Four star-rating fields with unclear distinctions
Danger, Priority, Rating, and Vulnerability are four separate 5-star fields displayed side by side at the top of every pin. A first-time user won't understand the difference between "Rating" and "Priority," or why "Danger" and "Vulnerability" are separate. Consider consolidating, renaming, or adding tooltip explanations.

### Minor — STAGING badge overlaps page content when scrolling
The fixed top-center STAGING banner overlaps section headings and content (e.g. the Smithsonian section title) as the user scrolls down. This is a staging-environment-only cosmetic issue but it obscures real content during testing.

---

## Organize (`/dashboard/organize/`)

### Major — "Reorder Visually" onboarding tooltip persists across unrelated tabs
The tooltip explaining label display order appears and stays visible when switching to the Lists and Filters sub-tabs, where it is irrelevant. It should be dismissed or scoped only to the Labels / Display Order tab.

### Major — Filters tab stuck on "Loading your filters…" indefinitely
The Filters sub-tab (`?tab=filters`) shows a spinner that never resolves. Same pattern as the loading issues on pin detail — no timeout or error fallback.

---

## Trips (`/dashboard/trips/`)

### Major — Page heading says "Plan" but nav says "Trips"
The top navigation item is labeled **Trips**, but the section hero heading reads **Plan**. The breadcrumb on trip detail pages also says `← Plan`. These should all use the same label. Pick one (probably "Trips") and apply it consistently.

### Major — Sub-tab also named "Trips" creates confusing hierarchy
The top-level nav item is "Trips," which leads to a page called "Plan" with sub-tabs: Overview, **Trips**, Calendar. Having a sub-tab with the same name as the parent nav item is disorienting.

### Major — Onboarding itinerary tooltip appears even when activities already exist
The "ITINERARY — Activities are proposed or confirmed stops" tooltip fires on the Neopex trip detail page, which already has one activity. The tooltip should not appear if the user has already added activities.

### Minor — Trip member RSVP status label is cryptic
The Members panel shows `CREATOR   YES ↓` for the trip owner. "YES" is not labeled — it's unclear whether this is RSVP status, role, or something else. Adding a label (e.g. "RSVP: Yes") would make it self-explanatory.

---

## Memories (`/dashboard/memories/`)

### Major — Map cards have no user-assigned name
In the Maps tab, saved maps are titled only with their creation date ("Jul 11, 2026"). There is no visible way to name a map. The title should either be editable or default to something more descriptive.

### Major — Visits page shows raw GPS coordinates as location titles
Many visit cards in the Visits tab display raw coordinates as their title (e.g. `41°38'06.1"N 73°34'04.8"W`) instead of a place name. This occurs when the underlying pin has no name. The same fallback logic used elsewhere (reverse geocoded address, or "Unnamed location" with an edit prompt) should apply here.

### Minor — Journal entry shows redundant "Rating" label
Each journal entry shows the pin name followed by the word "Rating" as a secondary label, then stars. The label is redundant since the stars already convey it's a rating. Remove the label or replace it with the visit date.

---

## Safety (`/dashboard/safety/`)

### Major — "Emergency contacts: Not set" has no call to action
The Safety Settings page clearly shows `Emergency contacts: Not set`, but there is no button, link, or prompt to add them nearby. For a safety-critical feature this is a significant gap — users who reach this screen after setup may not realize contacts aren't configured, and there's no obvious next step. An inline "Add contacts" button should appear next to the "Not set" value.

### Minor — Empty state uses a red shield icon
The "No check-ins yet" empty state on the Safety Overview uses a red shield, which reads as an error or alert. An empty state for a feature not yet used should use a neutral or encouraging visual treatment, not a red icon typically associated with danger.

---

## Profile (`/dashboard/profile/`)

### Critical — Email address is visible in the public profile header
The user's email (`jess.a.mann@gmail.com`) appears in the profile hero, which is the publicly visible section of the profile page. Email addresses should not be displayed publicly. Move it to a private settings area, or gate it behind the "Only visible to you" section.

### Major — Bio/About text is duplicated
The user's bio ("Real men make art, combat toxicity, and embrace love.") appears twice: once in the profile hero and again in a dedicated "ABOUT" card below. One instance should be removed.

### Major — Recently viewed pins show raw GPS coordinates
Pins without a name appear in the "Recently viewed pins" and "Recently created pins" sections as raw coordinate strings (e.g. `39°08'19.1"N 84°31'59.8"...`). A fallback display name (address or "Unnamed pin") should be used throughout the UI consistently.

---

## Add Pin Dialog

### Major — "Delete" button appears in the creation dialog
The "Add Pin" dialog includes a red **Delete** button in the footer. This is logically impossible — the pin hasn't been created yet, so there is nothing to delete. This is confusing and potentially alarming. The Delete button should only appear in the edit dialog for existing pins.

### Minor — "View details" button appears in the creation dialog
Similarly, a "View details" button appears in the Add Pin footer. This should only be available after the pin has been saved.

---

## Global Search

### Minor — Duplicate-named pins show no differentiating info in results
Searching "hospital" returns five pins all titled "Hospital" with "Hospital" as the subtitle — no address, neighborhood, or other distinguishing info is shown. Users cannot tell which result to choose. Search results should include address or location context as a secondary line.

### Minor — "Unnamed Location" pins surface in search
Pins with no name appear in search results with "Unnamed Location" as both title and subtitle, which is unhelpful. These should either display their address as the title or be excluded from search results until they have a name.

---

## Notifications Panel

### Minor — Friend request notifications have no inline action buttons
A pending friend request notification ("Sarah wants to be your friend") shows no Accept or Decline button within the panel. Users must navigate away to act on it. Inline action buttons are the standard pattern for this type of notification.

---

## Summary Table

| Area | Critical | Major | Minor |
|---|---|---|---|
| Map | — | 1 | 1 |
| Pin Detail | 3 | 1 | 2 |
| Organize | — | 2 | — |
| Trips | — | 4 | 1 |
| Memories | — | 2 | 1 |
| Safety | — | 1 | 1 |
| Profile | 1 | 2 | — |
| Add Pin Dialog | — | 1 | 1 |
| Search | — | — | 2 |
| Notifications | — | — | 1 |
| **Total** | **4** | **14** | **9** |

---

## Top Priorities

1. **Fix the Google Maps API key for staging** — Street View error and broken photos are the most visually broken things on the site.
2. **Replace raw coordinates with place names everywhere** — Pin detail heading, profile recent pins, visits list, search results. This is a pervasive data presentation issue.
3. **Remove "Delete" from the Add Pin dialog** — Obvious logic error, potentially alarming.
4. **Email address on public profile** — Privacy issue that should be fixed before any wider rollout.
5. **Add a CTA to set emergency contacts in Safety Settings** — Safety-critical gap.
6. **Fix indefinitely-loading panels** — News, Building Characteristics, LoopNet, and Filters all need timeout/error states.
