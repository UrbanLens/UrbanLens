# Currently Planned Features
Features planned for this release.

## Smaller Features
* Include screenshots of the app in About page, and in the README.md file. [UL-16]
* UI: Edit category dialog [UL-146]
* UI: Bulk edit category dialog (buttons are awful) [UL-147]
* Add descriptions to badges that are pre-populated. [UL-245]
* Remove work account from github project. [UL-247]
* Pull additional google place info from some supported google takeout files (Reviews.json, and others?) [UL-262]
* Consider: handle temporary markers when a user pin exists on that exact point. [UL-263]
* Ensure proper attribution in the smaller maps we're showing around the site (main map should be correct already. Others may or may not need work, though.) [UL-264]
* Better selection UX for organize page (clicking row selects it, hide select boxes until hover or one row is selected, etc) [UL-265]
* Main map > Edit pin dialog should have link to view full pin details. [UL-266]

## Medium Features
* Audit the import process for security (unzip, etc) [UL-268]

## Larger Features
* Reduce duplicate code, remove legacy code, simplify codebase. [UL-30]
* Run bandit and AI vulnerability scans; integrate with CI/CD. [UL-31]

## Bug Fixes
* Starting map option: Remember doesn't appear to work. [UL-255]
* ~~When filtering the map by rating, I saw a single pin without a rating.~~ RESOLVED 2026-07-18 (`18d03c3d`): `filter_by_criteria`'s min/max_rating used `if x := ...:`, so a slider at 0 was silently ignored - min_rating=0 previously matched everything unfiltered (which reads as "I filtered by rating and saw an unrated pin"). Now min_rating=0 correctly matches every pin including unrated ones (0 is the floor, not a threshold, since there's no such thing as a stored rating=0 - see UL-296), and min_rating=1+ correctly excludes unrated pins. [UL-270]
* I somehow got myself into a filter being active that I couldn't identify? [UL-271]
* Quickly switching between map layers sometimes is weird. Foggy sat view, etc. (Foggy may have just been loading indicator??) [UL-273]
* Cache time needs adjustments for some pin details data. Load page, wait 10 minutes, reload page, some items are marked as "fresh" [UL-277]
* Bulk edit dialog: I'm not certain that shared properties are showing up (i.e. selecting 2 rows with the same icon, the icon should show up in the dialog) [UL-353]
* Wikipedia not showing up for some HRSH buildings. [UL-354]
* Map caching / loading seems to be less reliable at 8k+ pins. [UL-355]
    [UL] Cache MISS - fetching all pins from server
    VM96 map:1014 [UL] Fetching 1 tile(s) from server: *
    VM96 map:1055 [UL] Server returned 8495 new pin(s) for tile(s): *
    VM96 map:402 [UL] Cache write failed (QuotaExceededError) - pins will reload next visit
    _writeCache	@	VM96 map:402
    NOTE From Jess: I think this was actually unrelated to 8.5k pins, and instead related to a stale cache. Clearing the cache fixed the problem.

## Map Search Filtering Polish
* The view options in the toolbar need a new button for "street details". [UL-278]
---
* Changing badge icon / color in organize doesn't immediately trigger cache update. [UL-279]

## Optimizations / Latency
* Adding a pin to the map. [UL-36]
* Cache API results, like Street View and Satellite View images. [UL-113]

## Project Health
* Review AI-created unit tests. Eliminate useless ones to assist code coverage reports. [UL-38]
* Provide secondary safeguards for permissions. [UL-39]

## Features that need verification
* ~~Verify Feature: Possible issue with then pulling or displaying visit history entries.~~ RESOLVED 2026-07-19 (`0fc51d40`): the "Visit History" header badge rendered the current server-side page's slice length, not the true total - understated the count (and shifted around while paging/toggling children) for any pin with more than one page of visits. Fixed to match `_photo_gallery.html`'s sibling badge (`page_obj.paginator.count`). [UL-114]
* ~~Verify Feature: When performing google or brave searches, add the street name, city, and state to the search query as optional keywords, to help disambiguate with unrelated results.~~ VERIFIED-ALREADY-IMPLEMENTED 2026-07-19: `PinController.web_search`/`web_search_refresh` (`controllers/pin.py:609`) build the query via `Pin.get_unique_search_name(quote_name=True, quote_locality=True)` (`models/pin/model.py:549`), which appends street address, city/county, and state - `quote_locality` specifically wraps "city state" as one exact-phrase term so a generic street address doesn't match the same address in an unrelated city. Already has dedicated test coverage (`UniqueSearchNameQuoteLocalityTests`) whose docstring names this exact caller. No code change needed. [UL-117]
* Add metadata for emojis (i.e. icons) to aid in searching for them. [UL-12]
* ~~Clicking outside of a dialog closes it, which is great. But clicking in the dialog and dragging outside unexpectedly closes it.~~ VERIFIED-ALREADY-FIXED 2026-07-19 (`19b8baec`): a site-wide drag-guard in `themes/base.html` (mousedown/click backdrop tracking) already prevents this for every dialog; the add-pin dialog additionally carried a redundant duplicate copy of the same algorithm (removed, wired into the shared `data-closefn` mechanism instead). No dialog reproduces the bug. [UL-32]
* When in the main map and the trip details page, drag/drop of a pin shouldn't be as easy at higher zoom levels. Not sure what I want here. Confirmation dialog? Disable at higher zoom? [UL-33]
* When creating the community wiki entry for a pin, ensure we're not leaking user data to it that the user expects to be private. For instance, the community wiki entry should probably be titled based on the google place name, not the user's custom title. Perhaps we can offer a choice between the two when the user is creating only a single pin? [UL-26]
* Ensure non-anonymized urls do not exist at all. Users should not be able to access urls we don't want them to access, (like .../profile/2/, instead of the uuid). [UL-40]
* Properly set up pre-commit hooks for linting, type checking, and security scans. [UL-15]
* User settings don't seem to properly save. [UL-34]
* ~~Verify the UX for changing the kind of a badge (do other properties get updated too, and is that clear?)~~ RESOLVED 2026-07-19 (`02729c81`): other properties do get updated correctly (memberships migrate, parent/child hierarchy is cleared since it only makes sense within one kind, pin marker caches invalidate, protected labels are blocked) - but it wasn't clear: the edit form's hint only mentioned memberships, saying nothing about hierarchy loss. Added a conditional warning shown only when the label actually has a parent or child. [UL-155]
* ~~Verify: Child trips work as expected.~~ RESOLVED 2026-07-19 (`9b376efd`): the feature is fully built (link picker, autocomplete search, ghost markers on the parent map), contrary to ROADMAP.md's "unverified/undesigned" note - but had two privacy leaks: `child_trip_uuid` resolution in the create/edit endpoints had no membership scoping (any trip could be linked, not just ones the user belongs to, unlike the picker's own search endpoint), and ghost markers never checked the child activity's `location_hidden` flag, unlike the identical check already applied to the parent trip's own activities. Both fixed. [UL-228]
* ~~Password reset should work elegantly with SSO users.~~ RESOLVED 2026-07-19 (`def2c4d6`): SSO-only accounts (no usable password) were silently dropped by Django's stock `PasswordResetForm.get_users()` while the view showed the same "check your email" success page either way - so they were told it worked and got nothing. Added `SsoAwarePasswordResetForm` (includes SSO-only accounts, routes them to a distinct email naming their sign-in provider) while preserving anti-enumeration. Also fixed the actual reason none of the app's branded `registration/*` templates were rendering at all: `TEMPLATES["DIRS"]` was empty, so `django.contrib.admin`/`auth`'s bundled templates of the same name silently won (they're registered ahead of `dashboard` in `INSTALLED_APPS`). [UL-257]
* Celery / async tasks: Move slow operations (API calls, geocoding, import jobs) to Celery tasks; all non-instant UI operations must show a progress indicator and use toast notifications on completion or failure [UL-119]

# Future Features
Features planned for future releases.

* Clean up ui on page dashboard/site-admin/subscriptions/ (bad padding, save buttons should go away for autosave, flex or grid adjustments, active grant actions being visible by default is confusing.) [UL-352]
* On the profile page, if the user has instagram linked, then show a section with the most recent instagram posts for that user. [UL-44]
* On the pin details and location wiki pages, we want to be able to show the property ownership records. However, there is not a consistent database to access that information that I'm aware of. Therefore, we need to have a strategy for looking up that information per county. To accomplish this, we need to create code to use AI to determine where to find that information for the given county, attempt to access the record for the given location, and if that's successful, then save the strategy used to the DB so that the same strategy can be used for other addresses in that county in the future. [UL-46]
* Full explanation (user friendly) to setup the app [UL-50]
* Implement a report button for comments and other user content. I'm not certain how this should work given there cannot be a manual moderation system, by design, since the moderator would then be able to see pins they shouldn't otherwise be able to see. Some ideas: we could allow manual moderation but mask some details (including pin coordinates). We could allow manual moderation of comments and images in isolation without sharing pin details. We could implement a community-driven moderation system without giving access to anyone who can't already see the content. [UL-51]
* PARTIALLY RESOLVED 2026-07-19 (`87ae885a`): API cost tracking and reporting. We should keep track of estimated costs for each individual user, so that future reporting strategies can be implemented that have access to legacy data. Track "hours used", and page loads for estimated CPU load cost, and track API costs by their actual cost at time of use. Landed: `ApiCallLog.cost_estimate` populated per-call from a new `ServiceDefaults.cost_per_call` registry (seeded for `google_geocoding` only - a real, verifiable rate; the other 50+ paid services still need their published rates added), aggregated into the site-admin API usage report. NOT yet done: per-user cost attribution (`ApiCallLog` has no user/profile FK at all - every gateway call is currently anonymous with respect to who triggered it, and many run in shared/background contexts with no single attributable user), and CPU/page-load cost estimation (hours-used, page-load compute cost) - both are real follow-up work, not covered by this pass. [UL-52]
* RESOLVED 2026-07-19 (`87ae885a`): Public "costs" reporting page for accountability, showing only combined costs for all users. Added `/costs/` (public, no login) showing the aggregate 30-day estimated cost and its per-priced-service breakdown - never per-user, matching this ticket's "combined... for all users" framing. [UL-53]
* Integrate gotify (?) for notifications to site admin. [UL-57]
* Allow users to vote on making a location "public", which would share it to all users (if those users wish). This requires substantial thought to get just right. The upside is that it would substantially encourage use of the app for new users, who haven't yet imported their own pins, by pre-populating their map with well known locations that are not vulnerable. [UL-58]
* "Get directions" button to send directions to their phone (or show on screen). [UL-59]
* AI suggestions on the trip planning page for when to schedule activities, taking into account drive time and user voting. AI suggestions of pins to add that are relevant to the trip. Etc. [UL-60]
* Trip planning page should have some ability to go to the pin details page (or location wiki) for each activity. [UL-61]
* Better UI form fields (sliders, date pickers, etc). [UL-63]
* Gallery photos can have additional metadata, including: an angle of view, floor, room, etc. [UL-65]
* Types of friends: "connections", "friends", "close friends", etc? I'm not sure this is needed in light of people badges. However, the mobile app idea of "connect with explorer" would encourage adding someone as a connection without necessarily wanting them to be a friend. I suppose this is also useful in the web app if you regularly encounter someone you may want to DM or keep track of, but don't want them to be impacted by your privacy and sharing settings. [UL-66]
* Outside of app error logging. Alerts on certain kinds of errors. [UL-69]
* Address DDOS, spamming, etc. [UL-70]
* Hypothesis unit tests: Add property-based tests wherever possible. [UL-120]
* Discord Integration [UL-29]
* Setup bug tracking (github issues?) [UL-1]
* Implement "hide user", and "mute user" features, alongside the existing "block user" feature. [UL-27]
* Proper CI/CD pipeline, tags, releases, etc. [UL-25]
* Support non-USA formats for dates, currency, distances via user settings. [UL-131]
* Support non-English language. [UL-132]
* User stats page (fun stats about the user: breakdown of pins by continent, etc). [UL-133]
* During site setup, tests for features (i.e. "send test email" button) [UL-135]
* Ensure rotating logs, purging cache data, etc, in the event of hacking incident. [UL-136]
* Review API Key restrictions for cloud providers (e.g. referrer restrictions for google, etc) [UL-137]
* Use remote secret store (maybe?) [UL-138]
* Ensure mobile-first. [UL-7]
* On pin details page: Google Places information section, showing Google's place name, nearby photos, extra street view / 360 / etc views, google reviews, website, etc. [UL-157]
* If a website exists, check if it is defunct, and check for recent activity. [UL-158]
* Add google place name, organization name, etc to aliases automatically. [UL-159]
* Yelp reviews. [UL-160]
* XLS import [UL-162]
* Ensure AI sandboxing. This isn't really necessary now, but would be necessary prior to any MCP usage for security reasons, and would also allow for local AI models. (ollama, etc) [UL-163]
* Badges that are created automatically: start them in a sensible priority order [UL-167]
* Organize Page: Move badge to child of another just by dragging (maybe??) [UL-169]
* Better emojis for: legal stuff (admission ticket, museum), underground, tunnel, sewer grate, hardhat. Verify we have: religions, languages, countries, urbex gear (boots, flashlight, backpack), photography stuff, time/calendar stuff (seasons?), greek letters, shapes (square, triangle, etc), ceramic tile (mosaic, etc), eyeglasses, book, magnifying glass, share symbol, muscle icon, weights, ninja, gavel, snake eating itself, better "repeat" arrow, "tag" icon (i.e. 'labelled'), save symbol, fleur d'lis [UL-170]
* "Recently used emojis" to make selecting them easier. [UL-172]
* dashboard/models/badges/model.py > Icons should probably be organized elsewhere. We probably want more elegant solution for defining all 3 traits for all of them (emoji, name, keywords). Many don't have keywords currently. [UL-174]
* When creating new badges during pin import, allow an AI to select an emoji and color for it. [UL-176]
* More (or all?) vector emojis that can change color. [UL-177]
* Limit username changes to prevent users from pretending to be someone else in comments, etc. (Perhaps track historical usernames and display them on the public profile? I'm not sure about this.) [UL-145]
* ~~BUG: Very first login form on first site install says "welcome back"~~ RESOLVED 2026-07-18 (`e9d374c5`): CustomLoginView now adds `is_first_run` (derived from `SiteSettings.bootstrap_admin_onboarding_complete`, not a plain user-exists check - registration creates the account before login is ever reached) so the page shows "Welcome to UrbanLens" through the whole first-run window instead. [UL-179]
* Onboarding: first map load -> "This is your first time using the map. Would you like to import any pins?" [UL-181]
* Pin import dialog in dark mode: Section header and pin rows blend too much. They should be separately conceptually (indent, border, bg color, etc) [UL-182]
* Allow bulk-selecting pins to add them to a campus as detail pins. [UL-183]
* UI: Tiny "saved" notice on settings pages should be better distinguished. [UL-184]
* Map Layer: Show/Hide Street Details (otherwise does not show on sat view) [UL-185]
* Main map: Some ability to go "back to home" quickly. [UL-187]
* UI Bug: Bulk edit dialog -> visual bug for parent categories without an icon with respect to the tag chip and selector. [UL-190]
* Organize page: Confirm before deleting badge with pins. [UL-192]
* ~~Bulk editing pins (based on search, badges, etc). For instance: Bulk set rating.~~ RESOLVED 2026-07-19 (`64c04fd8`, `d42e0be8`): added bulk rating (1-5 sets every selected pin's Review, 0 clears it, matching `PinEditView`'s single-pin semantics) plus a select in the bulk-edit dialog to trigger it. Found and fixed a real pre-existing bug along the way (`616215c6`): the single-pin "clear rating" button never actually deleted the Review row due to a broken sentinel check. [UL-193]
* Main Map: When searching, show loading overlay [UL-194]
* Organize > Merge Dialog -> Make an effort to choose the best merge candidate. (The one with an icon, then most pins). Is this done already?? [UL-198]
* Organize Page -> Allow reordering kind tabs somehow, to make understanding the feature set more accessible. [UL-200]
* Organize Page > Edit Badge -> The first parent badges to show are the ones already selected. [UL-201]
* ~~BUG: Not able to read all takeout files. For example: Parking.csv~~ RESOLVED 2026-07-19 (`d6a677a9`): the CSV importer only recognized a column literally named "URL"; Google Takeout's Parking.csv export uses "Parking location" instead, and has no latitude/longitude columns for the fallback to catch either, so every row silently failed and the file produced zero pins. Broadened the check to a candidate-column list. Note: the exact "Parking location" header name is based on the known Google Takeout Parking export format, not a sample file from this report - re-open if a real Parking.csv still fails after this fix. [UL-203]
* Create task to ensure vestigial assets are deleted (e.g. if they were supposed to be deleted already, but there was an error - such as for pin imports, exports, etc). [UL-205]
* BUG: Map import dialog, existing pins still show "new" in the row. [UL-206]
* ~~Verify: User imports pins without names, then imports "Labelled Places.json" with the same pins, the names of the originally created pins are updated.~~ RESOLVED 2026-07-19 (`e8eaf227`): real bug - `get_nearby_or_create`'s `defaults` only apply when creating a new pin, never to an existing one it merges into, so a nameless pin never picked up a name from a later import. Fixed to fill in a blank, non-user-provided name on merge, gated on `name_is_user_provided` (already documented for exactly this: "external API naming refreshes may replace placeholder/auto-generated labels only while this is False"). [UL-207]
* Main map: add pin dialog -> tags and categories picker has them in 2 sections, instead of standardized picker with other badge kinds and a search. Icon section is empty (no options). No option to make it private. Overall: This dialog should reuse existing components instead of redefining the dialog features. [UL-210]
* BUG: Something I did caused a new pin to be created with a badge named "Unknown". My workflow started with the creation of a new pin by right-clicking on the main map. [UL-212]
* Export Feature: Additional method of delivery in case the page is reloaded or closed. [UL-218]
* ~~Handle case where user has a comment, someone else replies to it, and then the original comment is deleted.~~ RESOLVED 2026-07-18 (`ce4b6af1`): replies already survived (parent FK was SET_NULL, not CASCADE), but became indistinguishable from genuine top-level comments. Added Comment.parent_deleted (migration 0076) + a pre_delete signal flagging replies before the FK nulls, mirroring the existing map_removed pattern; the comment panel now shows "Replying to a comment that was deleted" above the orphaned reply instead of silently losing its thread context. [UL-219]
* ~~BUG: When loading main map, it initially loads a different location than the starting point, then after a second it refreshes.~~ RESOLVED-PENDING-VERIFICATION 2026-07-18 (`82f11178`): for a returning GPS-mode user, the geolocation success callback unconditionally re-centered the live map once a fresh fix resolved, even when a cached position had already seeded the initial view - contradicting the code's own comment that already promised no visible jump in that case. Now only re-centers when no cache was used. This is client-side JS; only the fix's presence in the rendered page was verified, not actual browser behavior - please confirm in a real browser. [UL-221]
* "Import from map" feature to load pins from a different service (mapquest, google custom map, etc). Maybe? Does this encourage pin hoarding, or is it just useful? Is it even useful? [UL-222]
* If task UL-222 is implemented, then we could have a "subscribe to map" feature that would automatically pull updates. [UL-223]
* Gracefully handle slug changes when the pin (or location) name changes. This is relevant in cases where the slug was created with an incorrect or empty name, and we don't want to have its slug forever be "no-location" or "dropped-pin". [UL-226]
* Ensure dialogs that are closed have their data cleared (this occurred on the trip details page) [UL-229]
* Trip Detail Page > Add Pin Dialog: "Proposed / Confirmed" toggle looks weird. Hide location checkbox doesn't have an active state. Explanation of hide location should be a tooltip, not raw text below. The option for a Child Trip is great, but it should replace the pin selection area, not look like it's a separate option from pin selection. [UL-230]
* Trip Detail Page > Activities: After adding an activity with "hidden", the user who added the activity can't see the pin. That user should be able to, regardless of privacy settings... but we should show a "hidden" icon to make it clear that others may not see it. [UL-231]
* UI Bug: Trip Details Page > Activity section: When no confirmed activities exist, and you click on the activity tab, the content section seems to disappear, rather than existing with no content. [UL-233]
* Handle case where a user is invited via one email address, but joins the site using a different email. [UL-235]
* When a user signs up from an email invite link, they shouldn't need to verify their email again (assuming they provide the same email as the invite link was sent out to). [UL-236]
* Friend request pipeline needs UX work. Clicking notification does nothing, and the notification doesn't include an accept/reject button. Going to your profile, you see the accept/reject buttons there... great! clicking accept makes the section go away (great!) but the friends section isn't refreshed to show the new connection, so the user is left confused if it worked or not. Dotted line is distracting. Add label dropdown isn't closed when clicking somewhere else. Hovering over stars doesn't show the filled in stars (this must reuse existing components, not reinvent the wheel). [UL-237]
* On the public profile page, if "nothing in common yet", then hide the section. Buttons need ui work in dark mode. Private notes section needs stand-out color to distinguish it. [UL-238]
* ~~When logging out and then logging in as a new user, the cache was reused for that new user's map. That shouldn't happen. The cache needs to be tied to the current user and only used when that user is logged in.~~ RESOLVED 2026-07-18 (`7f612479`): the pin/layer caches were already profile-scoped, but three `LocationSearchEngine` search-history keys (main map address search, comment-map composer search, safety check-in destination search) were hardcoded, unscoped localStorage keys - fixed to include the viewing profile's id/uuid, with a one-time cleanup of the stale unscoped entry. [UL-239]
* Loopnet API or Scraping [UL-248]
* Get pin / location bounding box from external service (i.e. property boundaries), or attempt ML building boundaries detection. [UL-249]
* Consider feature: on main map, the icon and the circle could be pulled from different places, allowing 2 pieces of information to be displayed about each pin. [UL-251]
* "Organize a meetup", which would encourage a larger audience, encourage invitees to invite friends, etc. To prevent abuse, possibly: meetup pin would only be shown to those who already had it, and invitees could vote on whether it was too vulnerable to share? Idk. [UL-283]
* Max zoom out on the map still isn't quite right. Try clamping? [UL-285]
* Offline maps: mimicing other maps offline features, but tailored for areas around your known pins. For instance: offline maps for a trip would save data around each trip pin, entrance info, directions, etc, without needing to save offline info for the entire city. [UL-287]
* ~~pages/location/index.html and pages/location/satellite_view.html seem to have duplicate code. Confirm.~~ RESOLVED 2026-07-19 (`45db854e`): those two templates aren't duplicated (index.html just holds the standard HTMX auto-load skeleton for the satellite_view.html fragment) - the real duplication was in `PinController`: `satellite_view_carousell()` and `street_view()` were two near-identical ~50-line methods. Extracted a shared `_render_media_carousel()` helper. [UL-288]
* Move inline JS into separate TS files for performance, maintainability, typescript. [UL-289]
* Reorganize template partials [UL-292]
* AI chat assistant to find, organize (add/remove badges), pin, etc. e.g. "Plan a trip to Washington DC" -> find 5 pins in DC that aren't visited, create trip, etc. Perhaps ask questions about invitees, visited/not visited, etc. [UL-293]
* Convert remaining external services to plugins (weather, geocoding, search providers, routexl, wayback, overpass, datagov, digital commonwealth, apple maps, google earth, openhistoricalmap) [UL-294]
* Automatically mark nearby PD, public parking, etc. [UL-295]
* ~~On the main map > filter sidepanel, sliders don't account for 0 (e.g. "unrated")~~ RESOLVED 2026-07-18 (`18d03c3d`): `filter_by_criteria`'s min/max_rating AND min/max_danger used walrus-truthiness (`if x := criteria.get(...):`), which treats 0 as "not set" and skips the filter entirely. Fixed to `is not None`; rating additionally needed 0 special-cased as "unrated" (`reviews__isnull=True` for max_rating=0) since the app never persists an actual `Review.rating=0` row - `pin_edit.py` deletes the review instead. Danger needed only the simple fix, since it's a plain always-populated field where 0 is a real value. [UL-296]
* Enable file watch in docker compose for development -> https://docs.docker.com/compose/how-tos/file-watch/ [UL-297]
* "max members per trip" is not really the problem... "max pin shares per time period" is. We need to track and cap that instead, including through trips. [UL-299]
* After invite -> Edit profile doesn't visually look very good. [UL-300]
* Bookmark to add a pin to the menu for quick access (maybe?) [UL-301]
* Tagging users in photos, and automatic face redactions based on user preferences. [UL-303]
* Allow multiple email addresses to make it easier for other users to find you. [UL-304]
* Allow searching by social media handle (maybe? We definitely need a user preference to allow this) [UL-305]
* Consider: "anonymize me" setting. [UL-309]
* User setting for "make pins always private" - unless they manually attach to a location. [UL-310]
* ~~Create visit entry by geolocation.~~ VERIFIED-ALREADY-IMPLEMENTED 2026-07-19: `services.visits.record_geolocation_pin_visits()` already creates a `PinVisit(source=GEOLOCATION)` for every one of the profile's pins whose property boundary contains the device's current point (one per pin per calendar day), wired end-to-end from `MapController`'s geolocation endpoint through to `map/index.html`'s live GPS success callback (`_recordGeolocationVisit`, gated on the user's geolocation-tracking privacy setting). Already has full test coverage (`test_geolocation_visits.py`, 4/4 passing). No code change needed. [UL-312]
* Specify / change API keys in admin settings (e.g. api key rotated, but we don't want to reboot to load new env) [UL-313]
* In the pin import dialog, allow unselect all / select all for each section, and "make all private" type functionality. Also allow applying badges to every section at once (maybe). [UL-356]
* Pin Details Page: Google Image Search [UL-357]
* Pin Details Page: Instagram location-tagged posts (subscription required? to discourage location-tagging). [UL-358]
* When importing kmz, the suggested badge is "doc". It should be the filename. [UL-359]
* Trip List > New Trip Dialog: Suggested title is "Detroit factory run". Generate a large number of other suggestions, so this doesn't get stale, and encourages trip planning. Also, trip name shouldn't be required. [UL-360]
* ~~Connect with immich / google photos / etc to automatically grab visit info based on timestamps and coordinate metadata.~~ VERIFIED-ALREADY-IMPLEMENTED 2026-07-19 (with one platform-blocked exception): every import path (Immich, Google Photos, Flickr, plus locally-uploaded photos) already calls `services.memories.photos.log_visit_on_pin()`, which auto-creates a `PinVisit(source=PHOTO)` from the photo's own capture timestamp. Coordinate-metadata clustering to suggest brand-new pins from an entire library also already exists (`tasks.sweep_immich_library_locations` -> `services.pin_suggestions.ingest_location_hits`, 70 existing tests) for Immich and for locally-uploaded photos (`PinSuggestionOrigin.LOCAL_SCAN`). The one piece that's genuinely not built - a Google Photos equivalent of the full-library sweep - isn't an engineering gap: Google's Photos **Picker API** (the only API Google currently offers for third-party access) has no library-wide search/listing capability at all, by design - the user must manually pick photos in Google's own hosted UI (see `controllers/google_photos.py`'s own docstring: "there is no coordinate filter to apply here"). A sweep isn't buildable against that API. [UL-361]
* Audit for XSS risks related to badge names, and all other fields, etc [UL-362]
* Cleanup TODO file (This file). Remove completed, verify features, etc. [UL-363]
* Jinja templates (or html partials??) for emails. [UL-364]
* Extract javascript into TS files (this may already be a TODO item elsewhere in this file). [UL-289]
* Investigate: smaller css file for mobile to reduce mobile data usage. [UL-365]
* Better css minification broadly (will surely require more packaging/compiling steps) [UL-366]
* More unit tests specifically aiming at security / injection / etc [UL-367]
* Integration tests [UL-368]
* Property-based hypothesis tests everywhere. In addition to: coverage report for non-hypothesis tests only. That way, unit tests can be separated out into buckets: AI-generated tests, hypothesis tests, human-written tests. Coverage reports for each can be generated. This ensures that all fn/methods are fully property tested, AI-generated tests can attempt to cover the entire codebase, but that bad AI-generated tests and/or property tests don't report coverage of features that a human has not actually reviewed to be sure they are properly covered. [UL-369]
* Cleanup old/deprecated assets (old images, icons, etc) [UL-370]
* Safety-checkins page after the checkin was missed: it's great (and necessary) that contacts can view the page without having an account. However, this creates a certain gap in controlling access to the site. Review how we're doing it, especially with respect to: 2 browsers open the page using the same token, user tries opening the page with the wrong token, or with a right token prior to emails going out (this shouldn't be possible), etc. Also, ensure we are fully communicating to the primary user who created the checkin exactly what information will be shown, and when (perhaps encourage them to view the page as their contacts will see it after they create the checkin?) In the exact opposite direction, consider how to provide more information to emergency contacts, perhaps on a time delay. For instance: X hours after the emails go out, update the page with more information about the user's last known location, other pins they have in the area, etc? We'd have to be very careful about handling all this appropriately and communicating it to the user, with privacy controls. [UL-371]
* Investigate: import pin data into google my maps. (If not: then consider other services) [UL-372]
* Email export data to the user feature, so that data can be persisted even without the server online. (Alternatively: dropbox, meta, etc) [UL-373]
* Celery tasks for external APIs which are rate limited could be queued for later. [UL-374]
* User settings page: AI Features section needs better explanations. [UL-375]
* Create TOS -> I'm one person, please don't sue me. Safety checkin is best effort. For legal reasons, this site cannot advocate doing anything illegal. [UL-376]
* More targetted exports. For instance: exporting all pins that match a certain search, or exporting just a list of pins (once lists are implemented). This would allow importing select things into another app without importing everything. [UL-377]
* Markup: Dotted lines. [UL-378]
* Badge merge dialog: Show a big, obvious visual displaying what badges will go into what other badge, and which badges will no longer exist. [UL-379]
* On pin details page, allow dragging map vertically bigger, which saves between sessions. [UL-380]
* Turning off the markup layer on a map should turn off showing boundaries. [UL-381]
* Allow exporting data as other formats: KML, GPX, GeoJSON, CSV. [UL-382]
* "Promote child pin to parent pin" feature. [UL-383]
* Pin Details page > Edit dialog - The Pin type dropdown should probably go away. [UL-384]
* ~~One time: I'm not seeing the community wiki section appear on a particular pin details page. (4143533n-7355362w)~~ RESOLVED 2026-07-19 (`f45ba6b3`): real bug, and an existing pre-written (but not yet fixed) test named the exact mechanism - `PinOverviewView.get()` backfills a legacy Location's missing slug, but the "Create Community Wiki" button lives in the page hero, outside `#pin-overview`, so an already-loaded page kept showing "no wiki" until a full reload even right after the backfill. Fixed by having the overview response also carry an out-of-band hero re-render, matching the existing `_trip_hero_oob()` pattern. This pin was very likely hitting a legacy Location row that predated slug generation. [UL-385]
* Import pins is a little clunky. [UL-386]
* "Import my instagram" feature to port over photos you've posted. [UL-387]
* Find a way to make it easy to identify and/or fix situations where you have other pins on the main map which fall within the boundaries of another one of your pins. [UL-388]
* On user's profile page, show uploaded photos. Those photos will display to other users who have access to see them on something they're attached to. (e.g. photos uploaded to a wiki they also have).
* When moving/promoting child pin: "You already have a top-level pin at that location". We should allow merging the top-level pin right from that dialog.

## Really Big Ideas / Features
* Native android / ios apps (allowing expansion into additional features). [UL-72]
* Visualize a location, room, etc, by browsing similar photos chronologically in a visually stimulating way. [UL-73]
* Social media features (sharing content, stories, etc), allowing users to share content they want other explorers to see, but don't want to be publicly available on the internet to a non-exploring audience. [UL-74]
* Buffer features (maybe using their api?) for buffer-like functionality that's tailored toward exploring workflows. (This may be straying too much from the core app purpose) [UL-75]
* Look into decentralized stuff. [UL-76]
* Sync with some other service. (I don't think google maps is possible - see the section "issues I don't think are solvable" - but other services may be possible). This would provide a portion of a backup strategy for user data [UL-77]
* Kubernetes [UL-78]
* More API access for finding vintage photos and documents, location details, alerts, etc. [UL-79]
* Reduce reliance on javascript further by migrating more of it to HTMX. [UL-80]
* "Demolition Alert" feature. I'm not sure if this is practically possible, since regularly searching for every pin is out of the question. Allow subscribed users to set alerts on specific pins. [UL-81]
* Discord Bot (for known demolition updates? "note: the location you're discussing recently had security updates"? actions: "@bot plan trip")
* API for CRIS
* Integrate with instagram (instagram makes this extremely annoying). This would allow, for instance, importing all your instagram photos to jumpstart documenting the places you've been. [UL-390]
* Flaresolver (and similar), plus Tor for retrieving some API data (such as county tax records, etc). I'm not currently aware of anywhere this is needed yet, but surely there is somewhere it would be helpful and improve data quality or access.

### Native Mobile App
* Automatically check off visit logs [UL-82]
* "Who is here?" ping feature, allowing other users with the app to opt in to sharing their location. This solves the "I hear footsteps" problem. [UL-83]
* Track trip progress via gps, device motion, etc. This allows the user to remember what route they took, and could help address mapping tunnels. [UL-84]
* "Emergency device lock" feature, similar to an app from the ACLU, which turns on recording, disables notifications on the homescreen, disables fingerprint and face unlock, etc. [UL-86]
* "Location Warning" feature, allowing users to set a warning radius around their location, and other users in that radius can be notified (if they wish). [UL-87]
* "People on site group chat". [UL-88]
* Trip participants 'share my location'" feature to regroup after you split up. (opt-in) [UL-89]
* "Take and immediately upload" photo feature for trips, (or ?maybe? for community locations). This allows group trips to tell the other participants: "come over to this room to see this thing" quickly. [UL-90]
* Integrate minor photopills features. [UL-91]
* "Exploring Mode" changes notification sounds to subtle, ambient sounds. [UL-92]
* "Connect with explorer" feature when encountering someone new. Could also support connecting with non-app users somehow, or at least creating a note about the connection. [UL-93]

#### Crazy Stuff
This could be a playground for implementing a few exploratory ideas I've had in my head for a while.
* Person scanning via wifi [UL-94]
* Connect to other friendly devices (mobile ip camera, etc) [UL-95]
* Detect cameras, sensors nearby. [UL-96]
* Scan emergency frequencies to notify of issues. [UL-97]
* Notification for "exit time before sundown" or similar? [UL-98]

## Ideas to Consider
* Link to (or pull more data from) google maps, openstreetmap, mapquest, etc. [UL-99]
* Keep track of "encountered" users when using the app. This allows display of a fun stat: "first encountered", allows looking up people you've seen before but didn't connect with, and encourages social interaction. This would also facilitate restricting access to a user's profile unless they have been "encountered" by the current user (i.e. the user could not just type in a url with the user's slug, or be given a url with their uuid. Instead, they'd have to invite a connection with the user first, by email address, and allow the other user to opt in to the interaction.) [UL-100]
* Consider adding privacy controls to explicitly hide content from certain types of users, which would override the whitelist privacy controls the user set. For instance: "Show pins to users with 1 trip in common" and "hide pins from users with a specific badge" would give more control over privacy and sharing. I'm not sure how to do this in a way where the UI isn't overly complex and clunky. (maybe "advanced privacy controls"?) [UL-101]

## Issues I don't think are solvable
* Encrypting user data so the site admin doesn't have access to it. The only two solutions I can think of are (1) a peer-to-peer sharing system, or (2) separating the app into a "server" and "agent" app, wherein the client app has unencrypted data, but the server only has encrypted data. For (2), users would then be able to set up their own "agent" app on their own server, resulting in full ownership of their data. However, both solutions suffer from significant drawbacks. The latter is more attainable, but in order for the app to be usable for most users, we need a publicly hosted client app anyway, resulting in no privacy gains for most (or possibly for any) users. In order to consider the maximum benefit, it may be useful to calculate the time required to brute force gps coordinates, which it turns out is surprisingly small. In addition, both solutions suffer significant performance penalties, and technical complexity, for little to no gain. Finally, almost no users will understand the key differences between this problem being solved and not being solved, and will assume that data is unencrypted and visible to the site admin even if it is not. Therefore, I'm not certain that implementing it really improves user trust, while nonetheless encountering additional drawbacks. The main reason to do it seems to be to tell users we did it... which seems less beneficial than its cost. I'm undecided on this. [UL-102]
* Considerations about avoiding storing identifying user data. Given SSO, and a need to email the user, I'm not certain that this is solvable. 1-way hashing combined with a "verify your email before..." dialog could help address it, but that would only allow us to hash the email, not avoid storing it altogether, which would still make it crackable via brute force. In addition, it would interfere with our ability to email notifications. Users can give themselves full anonymity already by registering a new email address and choosing not to provide SSO or personal details during account creation. Providing those kinds of instructions might be helpful somewhere, and we could possibly provide a button on the profile page to allow them to anonymize their existing account in that way if they originally created their account the "wrong" way and want full anonymity going forward. [UL-103]
* Sync with google maps. Google maps does not allow labelling pins, or adding them to lists via a programmatic interface, and the only way to export data is through the google takeout system. The only way to mimic this would be through web scraping, which would be extremely fragile, and require users to grant way too many permissions to our app. Theoretically, this limitation could change in the future, depending entirely on google. [UL-104]
* Consider: Share with partner feature. I'm undecided on this... it would allow 1 user (or X users?) to share a large number of their pins. This probably encourages the wrong kind of behavior, but alternatively: it's a thing most explorers do in practice, and this would make that technical painpoint a lot easier. I'm leaning towards feeling that this isn't achievable in a responsible way. A half-measure could be to allow more sharing with "close friends", but that may also suffer from the same consequences (maybe even moreso).

## Issues requiring architectural solutions
* Allow users to interact with parts of the app (by invite?) without logging in. For instance, in the case of trip planning. [UL-105]
* Prevent users from "testing" if a location is abandoned by creating a test pin for it, then deleting said pin if no community wiki entry exists. Perhaps provide a delay before the community wiki entry is available to the user? Or cap pin creations? [UL-107]

## APIs to consider
* Wayback [UL-315]
* Apple Maps [UL-316]
* OpenHistoricalMap [UL-317]
* Tools listed by geohack: https://geohack.toolforge.org/geohack.php?pagename=White_House&params=38_53_52_N_77_02_11_W_type:landmark_region:US-DC [UL-318]
* USGS M2M / EarthExplorer [UL-319]
* USGS TNM API / topoView / HTMC [UL-320]
* Esri World Imagery Wayback [UL-321]
* OpenAerialMap [UL-322]

### More difficult
* ProQuest Digital Sanborn Maps [UL-323]
* Sanborn Maps on AWS / public datasets [UL-324]
* Map Warper / georeferenced map platforms [UL-325]
* State/county GIS portals [UL-326]
* LLM suggestion: build a provider abstraction like: coordinates → bbox → provider search → normalize result as {title, year, source, bounds, thumbnail, tile_url/download_url} [UL-327]

## Code Quality
### Fix Generics
* tags = Badge.objects.tags() (and also .categories()) -> Cannot access attribute "categories" for class "Manager" [UL-126]
* profile = user.profile -> Cannot access attribute "profile" for class "User" [UL-127]

## From README Roadmap (migrated)
Items previously listed in README.md that are not already tracked elsewhere in this file. Some of these may already be implemented, so this list should be looked over and pruned before being relied on.

### Data
* Collect pin information during import. [UL-328]
* Remove (or better integrate) pin status (visited vs "visited" tag vs visit history). [UL-329]

### Community
* User list (with privacy settings). [UL-331]

### UI - General
* Allow user to reorder pin details sections. [UL-333]
* Change default pin details sections order. [UL-334]

### UI - Pin Detail Page
* Map sometimes double scrolls (latency?). [UL-335]
* Fix satellite view (street view may also be broken?). [UL-336]
* Fix web results (web results filtering through AI?). [UL-337]
* Fix boundary markup, security indicators, and section visual separation. [UL-338]

### UI - Trip Details Page
* Pin icons (1, 2, ...) should better communicate the idea, rather than looking like grouping blobs from other maps. Also should still use custom icons. [UL-340]
* Multiple trip UX improvements: whitespace, archiving, notifications, RSVP, variations, organizers, end dates, activity editor. [UL-341]
* Add pins by clicking map, drag/drop, coordinate, or place lookup. [UL-342]
* Comments: image upload indication, reply buttons, fix comment count and delete duplication bug. [UL-343]
* Delete should probably not delete for everyone?
* Main trip page: use the whitespace. Calendar? etc?
* Allow archiving old events.
* Notify other users when changes.
* Trip variations (map markup, variation 1/2/3, etc).
* RSVP per activity.
* Users can click on the map to add a pin.
* Ability to drag and drop some pins on the map (especially ones that were added via coordinates or right clicking).
* Ability to add pins based on coordinate, not just geolookup addresses.
* Ability to add pins based on places lookup, maybe?
* Order activity list by date.
* Multiple organizers.
* Activity end dates.
* Trip settings: fix checkbox bug. Also, each option should have 3 states (no one, organizers, everyone).
* In activity edit dialog, add delete button.
* Add some additional descriptor for activities (an icon, or a category? For instance: Camping, Food).
* When comment has image, must be indication that image will be uploaded after it's selected from user's computer.
* Reply button beneath replies.

### APIs
* ~~Sunrise / sunset for weather.~~ RESOLVED 2026-07-19 (`034eec89`): added to the pin weather panel, plus an approximated golden-hour window (hour after sunrise / before sunset - the common photography-app convention). Always fetched via Open-Meteo (`timezone=auto` resolves local time server-side) independent of whether OpenWeatherMap serves the rest of the forecast, since OWM's 5-day endpoint has no sunrise/sunset field. [UL-345]
* Address is often incorrect Smithsonian results (AI filtering? Only names >= certain length?). [UL-389]

### Misc
* Viewing notifications in the dropdown should mark them read. Not just clicking on them. [UL-348]
* Hide "(schedule) Never" in pin popup for last visited. This is already implied. [UL-349]

## To Investigate
* When creating pin here: 39.15924, -84.68402... place name is "Mack", details ui sections are wonky, street view is black image. [UL-350]