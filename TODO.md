# Currently Planned Features
Features planned for this release.

## Smaller Features
* Cleanup git history, and begin using branches for dev. [UL-14]
* Include screenshots of the app in About page, and in the README.md file. [UL-16]
* Provide explanation of how to do a google takeout to import pins. (Possible onboarding process?) [UL-142]
* Ensure fully sanitized user input for pin names, location names, etc, which are passed into external urls. Require strict character sets, min/max lengths, and so on. [UL-143]
* UI: Edit category dialog [UL-146]
* UI: Bulk edit category dialog (buttons are awful) [UL-147]
* Add descriptions to badges that are pre-populated. [UL-245]
* Remove work account from github project. [UL-247]
* Pull additional google place info from some supported google takeout files (Reviews.json, and others?) [UL-262]
* Consider: handle temporary markers when a user pin exists on that exact point. [UL-263]
* Ensure proper attribution in the smaller maps we're showing around the site (main map should be correct already. Others may or may not need work, though.) [UL-264]
* Better selection UX for organize page (clicking row selects it, hide select boxes until hover or one row is selected, etc) [UL-265]
* Main map > Edit pin dialog should have link to view full pin details. [UL-266]
* Allow users to specify "nickname only" aliases, which are used when they search for their pins, but not used in API requests to external resources. (e.g. "The 'Fuck the birds' School") [UL-267]

## Medium Features
* Audit the import process for security (unzip, etc) [UL-268]

## Larger Features
* Reduce duplicate code, remove legacy code, simplify codebase. [UL-30]
* Run bandit and AI vulnerability scans; integrate with CI/CD. [UL-31]

## Bug Fixes
* During import pins, checking "create badge", the badge is created, but the pins aren't added to it. (They are added to already existing badges you select, though) [UL-150]
* UI Bug: Multi-select toolbar in dark mode [UL-151]
* Organize Page: Occasionally, after editing or merging badges, the edit button for other rows no longer opens the edit dialog. I'm not sure exactly what circumstances this happens. [UL-197]
* Badge Statuses can't be hierarchical?? (I guess they can, it just doesn't show in the organize status page ui) [UL-199]
* Trip Details > Adding Pin: The suggestions are only geocoded, not pin searches. [UL-227]
* Starting map option: Remember doesn't appear to work. [UL-255]
* Organize: Bulk edit button doesn't open dialog. [UL-269]
* When filtering the map by rating, I saw a single pin without a rating. [UL-270]
* I somehow got myself into a filter being active that I couldn't identify? [UL-271]
* When clearing a formula, it doesn't trigger a pin refresh. [UL-272]
* Quickly switching between map layers sometimes is weird. Foggy sat view, etc. (Foggy may have just been loading indicator??) [UL-273]
* After going to a suggested jump to point, clicking the temporary marker to create a pin, and submitting, the new pin doesn't show up on the map without a refresh. (maybe this was due to latency, which is a separate TODO item?) [UL-274]
* Pin details page: Plus buttons don't look good again. [UL-275]
* On pin details page (+ maybe location wiki), some circumstance with failing API cause latency across the entire site for ~30 seconds. (kartaview?) This is an issue with offloading these tasks to celery / running in background. [UL-276]
* Cache time needs adjustments for some pin details data. Load page, wait 10 minutes, reload page, some items are marked as "fresh" [UL-277]

## Map Search Filtering Polish
* The view options in the toolbar need a new button for "street details". [UL-278]
---
* Changing badge icon / color in organize doesn't immediately trigger cache update. [UL-279]
---
* Throughout site: tooltips clip (overflow: hidden) [UL-280]
* Consider again: Pin count while filtering [UL-281]

## Optimizations / Latency
* Adding a pin to the map. [UL-36]
* Cache API results, like Street View and Satellite View images. [UL-113]

## Project Health
* Review AI-created unit tests. Eliminate useless ones to assist code coverage reports. [UL-38]
* Provide secondary safeguards for permissions. [UL-39]

## Features that need verification
* password reset. [UL-41]
* Verify Feature: Possible issue with then pulling or displaying visit history entries. [UL-114]
* Verify Feature: On the pin details page, if the smithsonian archive section is empty, then hide it. [UL-115]
* Verify Feature: On the pin details page, there is a notes section and a comments section. But only one is needed. Keep comments, but remove the notes. Attempt to display a street address for the pin, assuming we can figure out what that address would be, and make sure that address is cached so we don't have to contact an external api multiple times. [UL-116]
* Verify Feature: When performing google or brave searches, add the street name, city, and state to the search query as optional keywords, to help disambiguate with unrelated results. [UL-117]
* Support partial cache updates, instead of refreshing the cache for all pins at once. [UL-22]
* Limit failed login attempts. [UL-28]
* Add metadata for emojis (i.e. icons) to aid in searching for them. [UL-12]
* When creating maps for comments, allow using satellite mode or topographic mode as well as the default view. [UL-13]
* Discord SSO [UL-11]
* "Don't leave page" dialog before a settings page is fully saved. [UL-9]
* Clicking outside of a dialog closes it, which is great. But clicking in the dialog and dragging outside unexpectedly closes it. [UL-32]
* On the public profile page, when saving a note, the note section is duplicated. [UL-112]
* When in the main map and the trip details page, drag/drop of a pin shouldn't be as easy at higher zoom levels. Not sure what I want here. Confirmation dialog? Disable at higher zoom? [UL-33]
* When creating the community wiki entry for a pin, ensure we're not leaking user data to it that the user expects to be private. For instance, the community wiki entry should probably be titled based on the google place name, not the user's custom title. Perhaps we can offer a choice between the two when the user is creating only a single pin? [UL-26]
* After converting a badge type, then switching tabs, the converted badge doesn't appear in the expected list. [UL-123]
* Ensure non-anonymized urls do not exist at all. Users should not be able to access urls we don't want them to access, (like .../profile/2/, instead of the uuid). [UL-40]
* Properly set up pre-commit hooks for linting, type checking, and security scans. [UL-15]
* Password Requirements should be reasonably strong. [UL-130]
* User settings don't seem to properly save. [UL-34]
* Verify the UX for changing the kind of a badge (do other properties get updated too, and is that clear?) [UL-155]
* ~~UI Bug in Dark Mode: Organize page -> Merge dialog doesn't show titles of cats being merged. [UL-191]~~
* Verify: Child trips work as expected. [UL-228]
* Password reset should go to application page, not django password reset page. [UL-256]
* Password reset should work elegantly with SSO users. [UL-257]
* Celery / async tasks: Move slow operations (API calls, geocoding, import jobs) to Celery tasks; all non-instant UI operations must show a progress indicator and use toast notifications on completion or failure [UL-119]
* In tools page, do only admins have access to the db backup tool? I guess we should probably move that to an admin-only page to be certain. (also, page width is wrong). [UL-282]

# Future Features
Features planned for future releases.

* On the profile page, if the user has instagram linked, then show a section with the most recent instagram posts for that user. [UL-44]
* Allow users to specify their signal username. Ideally, make it easy for users to add them to their signal contacts if it's provided. [UL-45]
* On the pin details and location wiki pages, we want to be able to show the property ownership records. However, there is not a consistent database to access that information that I'm aware of. Therefore, we need to have a strategy for looking up that information per county. To accomplish this, we need to create code to use AI to determine where to find that information for the given county, attempt to access the record for the given location, and if that's successful, then save the strategy used to the DB so that the same strategy can be used for other addresses in that county in the future. [UL-46]
* Visit logs can include photos or maps with markup, if the user desires. [UL-48]
* When user uploads a photo containing GPS data and a date, mark a visit log entry for that date, and attach the photo to it. [UL-49]
* Full explanation (user friendly) to setup the app [UL-50]
* Implement a report button for comments and other user content. I'm not certain how this should work given there cannot be a manual moderation system, by design, since the moderator would then be able to see pins they shouldn't otherwise be able to see. Some ideas: we could allow manual moderation but mask some details (including pin coordinates). We could allow manual moderation of comments and images in isolation without sharing pin details. We could implement a community-driven moderation system without giving access to anyone who can't already see the content. [UL-51]
* API cost tracking and reporting. We should keep track of estimated costs for each individual user, so that future reporting strategies can be implemented that have access to legacy data. Track "hours used", and page loads for estimated CPU load cost, and track API costs by their actual cost at time of use. [UL-52]
* Public "costs" reporting page for accountability, showing only combined costs for all users. [UL-53]
* Messaging between users. [UL-55]
* Request browser notification permissions for users. [UL-56]
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
* Saved map searches ("My Bucket List", etc). [UL-71]
* Allow importing of timeline data to mark pins as Visited [UL-118]
* Hypothesis unit tests: Add property-based tests wherever possible. [UL-120]
* Discord Integration [UL-29]
* Setup bug tracking (github issues?) [UL-1]
* Share button on pin details page to share with a specific friend. [UL-20]
* "Accept" / "Reject" shared pin for the user being shared with. [UL-21]
* Implement "hide user", and "mute user" features, alongside the existing "block user" feature. [UL-27]
* Proper CI/CD pipeline, tags, releases, etc. [UL-25]
* Support non-USA formats for dates, currency, distances via user settings. [UL-131]
* Support non-English language. [UL-132]
* User stats page (fun stats about the user: breakdown of pins by continent, etc). [UL-133]
* Lists (these aren't strictly necessary, due to badges, but could allow users to create lists of unrelated things. Like "my favorite explores in February" or "1 Best Church in Each State"). [UL-134]
* During site setup, tests for features (i.e. "send test email" button) [UL-135]
* Ensure rotating logs, purging cache data, etc, in the event of hacking incident. [UL-136]
* Review API Key restrictions for cloud providers (e.g. referrer restrictions for google, etc) [UL-137]
* Use remote secret store (maybe?) [UL-138]
* Automatic backups [UL-139]
* Users created with SSO should still get a password so they can login without SSO. [UL-156]
* Ensure mobile-first. [UL-7]
* On pin details page: Google Places information section, showing Google's place name, nearby photos, extra street view / 360 / etc views, google reviews, website, etc. [UL-157]
* If a website exists, check if it is defunct, and check for recent activity. [UL-158]
* Add google place name, organization name, etc to aliases automatically. [UL-159]
* Yelp reviews. [UL-160]
* Notepad import (AI Parsing) [UL-161]
* XLS import [UL-162]
* Ensure AI sandboxing. This isn't really necessary now, but would be necessary prior to any MCP usage for security reasons, and would also allow for local AI models. (ollama, etc) [UL-163]
* Create separate logged-in homepage (small map, widgets, links to other pages, commonly visited pins, etc) [UL-164]
* "View my profile as..." feature to help explain to user's how privacy settings are being applied. [UL-165]
* Badges that are created automatically: start them in a sensible priority order [UL-167]
* Organize Page: Move badge to child of another just by dragging (maybe??) [UL-169]
* Better emojis for: legal stuff (admission ticket, museum), underground, tunnel, sewer grate, hardhat. Verify we have: religions, languages, countries, urbex gear (boots, flashlight, backpack), photography stuff, time/calendar stuff (seasons?), greek letters, shapes (square, triangle, etc), ceramic tile (mosaic, etc), eyeglasses, book, magnifying glass, share symbol, muscle icon, weights, ninja, gavel, snake eating itself, better "repeat" arrow, "tag" icon (i.e. 'labelled'), save symbol, fleur d'lis [UL-170]
* "Recently used emojis" to make selecting them easier. [UL-172]
* Pre-populate example tags for new users (to help explain usage of site). "Notable", "Graffiti", "Photography", "Dangerous", "Popular" [UL-173]
* dashboard/models/badges/model.py > Icons should probably be organized elsewhere. We probably want more elegant solution for defining all 3 traits for all of them (emoji, name, keywords). Many don't have keywords currently. [UL-174]
* When creating badges (or editing them), consider adding another section to choose badges as children as well as parents. This could make it easier for users to make a large number of changes more quickly. [UL-175]
* When creating new badges during pin import, allow an AI to select an emoji and color for it. [UL-176]
* More (or all?) vector emojis that can change color. [UL-177]
* Limit username changes to prevent users from pretending to be someone else in comments, etc. (Perhaps track historical usernames and display them on the public profile? I'm not sure about this.) [UL-145]
* BUG: Very first login form on first site install says "welcome back" [UL-179]
* Onboarding: first map load -> "This is your first time using the map. Would you like to import any pins?" [UL-181]
* Pin import dialog in dark mode: Section header and pin rows blend too much. They should be separately conceptually (indent, border, bg color, etc) [UL-182]
* Allow bulk-selecting pins to add them to a campus as detail pins. [UL-183]
* UI: Tiny "saved" notice on settings pages should be better distinguished. [UL-184]
* Map Layer: Show/Hide Street Details (otherwise does not show on sat view) [UL-185]
* Main map: Some ability to go "back to home" quickly. [UL-187]
* UI Bug: Bulk edit dialog -> visual bug for parent categories without an icon with respect to the tag chip and selector. [UL-190]
* Organize page: Confirm before deleting badge with pins. [UL-192]
* Bulk editing pins (based on search, badges, etc). For instance: Bulk set rating. [UL-193]
* Main Map: When searching, show loading overlay [UL-194]
* Main map: Way to show pins as a list (particularly when searching). [UL-196]
* Organize > Merge Dialog -> Make an effort to choose the best merge candidate. (The one with an icon, then most pins). Is this done already?? [UL-198]
* Organize Page -> Allow reordering kind tabs somehow, to make understanding the feature set more accessible. [UL-200]
* Organize Page > Edit Badge -> The first parent badges to show are the ones already selected. [UL-201]
* BUG: Not able to read all takeout files. For example: Parking.csv [UL-203]
* Support importing kmz [UL-204]
* Create task to ensure vestigial assets are deleted (e.g. if they were supposed to be deleted already, but there was an error - such as for pin imports, exports, etc). [UL-205]
* BUG: Map import dialog, existing pins still show "new" in the row. [UL-206]
* Verify: User imports pins without names, then imports "Labelled Places.json" with the same pins, the names of the originally created pins are updated. [UL-207]
* ~~on client browser, const _PROFILE_ID = 1; is unnecessary and hints at vulnerabilities. [UL-208]~~
* Main map: add pin dialog -> tags and categories picker has them in 2 sections, instead of standardized picker with other badge kinds and a search. Icon section is empty (no options). No option to make it private. Overall: This dialog should reuse existing components instead of redefining the dialog features. [UL-210]
* BUG: Something I did caused a new pin to be created with a badge named "Unknown". My workflow started with the creation of a new pin by right-clicking on the main map. [UL-212]
* Add FAQ to about page. [UL-217]
* Export Feature: Additional method of delivery in case the page is reloaded or closed. [UL-218]
* Handle case where user has a comment, someone else replies to it, and then the original comment is deleted. [UL-219]
* New Site Tool -> Delete My Data: Fully removes all the user's submitted data from everywhere on the site. [UL-220]
* BUG: When loading main map, it initially loads a different location than the starting point, then after a second it refreshes. [UL-221]
* "Import from map" feature to load pins from a different service (mapquest, google custom map, etc). Maybe? Does this encourage pin hoarding, or is it just useful? Is it even useful? [UL-222]
* If task UL-222 is implemented, then we could have a "subscribe to map" feature that would automatically pull updates. [UL-223]
* Using haveibeenpwned, do not allow the use of compromised passwords. (Is this overreach? Probably not, but maybe. We want to ensure the security of the site and its data as much as possible.) [UL-224]
* On pin details page, when street view is unavailable, hide the section. [UL-225]
* Gracefully handle slug changes when the pin (or location) name changes. This is relevant in cases where the slug was created with an incorrect or empty name, and we don't want to have its slug forever be "no-location" or "dropped-pin". [UL-226]
* Ensure dialogs that are closed have their data cleared (this occurred on the trip details page) [UL-229]
* Trip Detail Page > Add Pin Dialog: "Proposed / Confirmed" toggle looks weird. Hide location checkbox doesn't have an active state. Explanation of hide location should be a tooltip, not raw text below. The option for a Child Trip is great, but it should replace the pin selection area, not look like it's a separate option from pin selection. [UL-230]
* Trip Detail Page > Activities: After adding an activity with "hidden", the user who added the activity can't see the pin. That user should be able to, regardless of privacy settings... but we should show a "hidden" icon to make it clear that others may not see it. [UL-231]
* UI Bug: Trip Details Page > Activity section: When no confirmed activities exist, and you click on the activity tab, the content section seems to disappear, rather than existing with no content. [UL-233]
* Implement a few extra "undos". For instance: pin deletion, etc. [UL-234]
* Handle case where a user is invited via one email address, but joins the site using a different email. [UL-235]
* When a user signs up from an email invite link, they shouldn't need to verify their email again (assuming they provide the same email as the invite link was sent out to). [UL-236]
* Friend request pipeline needs UX work. Clicking notification does nothing, and the notification doesn't include an accept/reject button. Going to your profile, you see the accept/reject buttons there... great! clicking accept makes the section go away (great!) but the friends section isn't refreshed to show the new connection, so the user is left confused if it worked or not. Dotted line is distracting. Add label dropdown isn't closed when clicking somewhere else. Hovering over stars doesn't show the filled in stars (this must reuse existing components, not reinvent the wheel). [UL-237]
* On the public profile page, if "nothing in common yet", then hide the section. Buttons need ui work in dark mode. Private notes section needs stand-out color to distinguish it. [UL-238]
* When logging out and then logging in as a new user, the cache was reused for that new user's map. That shouldn't happen. The cache needs to be tied to the current user and only used when that user is logged in. [UL-239]
* After accepting a friend request, mark the friend request notification read. This should probably happen when you view the friend request on your profile page at all. [UL-240]
* After accepting friend request, the old friend request notification shouldn't have accept or reject buttons anymore. [UL-241]
* When all notifications are marked read, the notification counter above the bell doesn't update until page refresh. [UL-242]
* When you are friends with a user, the "add friend" button on their profile shouldn't show up. Instead, it should be "remove friend". Implement the remove friend feature for this. [UL-243]
* Loopnet API or Scraping [UL-248]
* Get pin / location bounding box from external service (i.e. property boundaries), or attempt ML building boundaries detection. [UL-249]
* ~~Organize Page > Priority Tab: Provide mechanism for shifting an item to a specific position (e.g. "go to position 20")~~p, and allow multi-select before dragging to drag as a group. [UL-250]
* Consider feature: on main map, the icon and the circle could be pulled from different places, allowing 2 pieces of information to be displayed about each pin. [UL-251]
* "Organize a meetup", which would encourage a larger audience, encourage invitees to invite friends, etc. To prevent abuse, possibly: meetup pin would only be shown to those who already had it, and invitees could vote on whether it was too vulnerable to share? Idk. [UL-283]
* Only check US-centric APIs (like loopnet, NPS, etc) when the location is in the USA. [UL-284]
* Max zoom out on the map still isn't quite right. Try clamping? [UL-285]
* Undo pin deletion feature. (Initially mark the pin as deleted, but only realize the deletion in X days. User can undo the action in a new page.) [UL-286]
* Offline maps: mimicing other maps offline features, but tailored for areas around your known pins. For instance: offline maps for a trip would save data around each trip pin, entrance info, directions, etc, without needing to save offline info for the entire city. [UL-287]
* pages/location/index.html and pages/location/satellite_view.html seem to have duplicate code. Confirm. [UL-288]
* Move inline JS into separate TS files for performance, maintainability, typescript. [UL-289]
* Community wiki: Average Danger/Rating/Vulnerability scores when 5+ people have pinned it. [UL-290]
* Reorganize api services [UL-291]
* Reorganize template partials [UL-292]
* AI chat assistant to find, organize (add/remove badges), pin, etc. e.g. "Plan a trip to Washington DC" -> find 5 pins in DC that aren't visited, create trip, etc. Perhaps ask questions about invitees, visited/not visited, etc. [UL-293]
* Plugin system for APIs and Services (we're quickly amassing a lot, and individual installs may want to add and remove some of them) [UL-294]
* Automatically mark nearby PD, public parking, etc. [UL-295]
* On the main map > filter sidepanel, sliders don't account for 0 (e.g. "unrated") [UL-296]
* Enable file watch in docker compose for development -> https://docs.docker.com/compose/how-tos/file-watch/ [UL-297]
* Visit log can include "with another user", which logs it / visually shows it, and notifies the other user to optionally accept the visit log as well. [UL-298]
* "max members per trip" is not really the problem... "max pin shares per time period" is. We need to track and cap that instead, including through trips. [UL-299]
* After invite -> Edit profile doesn't visually look very good. [UL-300]
* Bookmark to add a pin to the menu for quick access (maybe?) [UL-301]
* 2FA [UL-302]
* Tagging users in photos, and automatic face redactions based on user preferences. [UL-303]
* Allow multiple email addresses to make it easier for other users to find you. [UL-304]
* Allow searching by social media handle (maybe? We definitely need a user preference to allow this) [UL-305]
* About / FAQ entries about privacy and consent. (no ips, no js tracking, consent first) [UL-306]
* User option to opt-out of visit entry tracking. [UL-307]
* Onboarding screen for "what do you care about", wherein they can opt out of everything privacy related. [UL-308]
* Consider: "anonymize me" setting. [UL-309]
* User setting for "make pins always private" - unless they manually attach to a location. [UL-310]
* "I didn't come home" feature can be implemented via email prior to mobile app. [UL-311]
* Create visit entry by geolocation. [UL-312]
* Specify / change API keys in admin settings (e.g. api key rotated, but we don't want to reboot to load new env) [UL-313]
* "Memories" page, showing location history and location visits over time. [UL-314]
* In the pin import dialog, allow unselect all / select all for each section, and "make all private" type functionality. Also allow applying badges to every section at once (maybe).
* Pin Details Page: Google Image Search
* Pin Details Page: Instagram location-tagged posts (subscription required? to discourage location-tagging).
* When importing kmz, the suggested badge is "doc". It should be the filename.
* Trip List > New Trip Dialog: Suggested title is "Detroit factory run". Generate a large number of other suggestions, so this doesn't get stale, and encourages trip planning. Also, trip name shouldn't be required.
* Onboarding: Set general privacy preferences, letting the user balance "Features" vs "Privacy". For instance: turn off all Visit History tracking.
* Connect with immich / google photos / etc to automatically grab visit info based on timestamps and coordinate metadata.
* Audit for XSS risks related to badge names, and all other fields, etc

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

### Native Mobile App
* Automatically check off visit logs [UL-82]
* "Who is here?" ping feature, allowing other users with the app to opt in to sharing their location. This solves the "I hear footsteps" problem. [UL-83]
* Track trip progress via gps, device motion, etc. This allows the user to remember what route they took, and could help address mapping tunnels. [UL-84]
* "I've been hurt" features, allowing users to keep their location secret unless they hit a button, or don't get back on the app after an explore, at which point their last known location and trip details are sent to emergency contacts. [UL-85]
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
* Ability to @ friends in comments, etc. [UL-330]
* User list (with privacy settings). [UL-331]
* Ability to view other user profiles by clicking on comments, etc. [UL-332]

### UI - General
* Allow user to reorder pin details sections. [UL-333]
* Change default pin details sections order. [UL-334]

### UI - Pin Detail Page
* Map sometimes double scrolls (latency?). [UL-335]
* Fix satellite view (street view may also be broken?). [UL-336]
* Fix web results (web results filtering through AI?). [UL-337]
* Fix boundary markup, security indicators, and section visual separation. [UL-338]
* When adding tags: the search bar needs padding. [UL-339]

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
* Leaving a trip should take you back to the trip list.
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
* Never say "0 minutes ago".
* When comment has image, must be indication that image will be uploaded after it's selected from user's computer.
* Reply button beneath replies.
* Bug: Comment count does not count replies.
* Bug: After deleting comment, the comment section duplicates itself.

### APIs
* Sunrise / sunset for weather. [UL-345]
* Address is often incorrect Smithsonian results (AI filtering? Only names >= certain length?).

### Misc
* User settings: Allow friend requests checkbox is missing. Should have additional option for "from users with one pin in common", "1 mutual", etc. [UL-346]
* Clicking on notification should take you to the relevant page. [UL-347]
* Viewing notifications in the dropdown should mark them read. Not just clicking on them. [UL-348]
* Hide "(schedule) Never" in pin popup for last visited. This is already implied. [UL-349]

## To Investigate
* When creating pin here: 39.15924, -84.68402... place name is "Mack", details ui sections are wonky, street view is black image. [UL-350]