# Currently Planned Features
Features planned for this release.

## UI Adjustments
* Loading indicators for all ui actions that take time. (Creating pin, searching map, etc) [UL-8]

## Smaller Features
* Cleanup git history, and begin using branches for dev. [UL-14]
* Include screenshots of the app in About page, and in the README.md file. [UL-16]
* Provide explanation of how to do a google takeout to import pins. (Possible onboarding process?) [UL-142]
* Ensure fully sanitized user input for pin names, location names, etc, which are passed into external urls. Require strict character sets, min/max lengths, and so on. [UL-143]
* Per the above, any of that data which gets passed to an AI needs additional safeguards against jailbreaking. [UL-144]
* UI: Edit category dialog [UL-146]
* UI: Bulk edit category dialog (buttons are awful) [UL-147]
* Add descriptions to badges that are pre-populated. [UL-245]
* Remove work account from github project. [UL-247]
* Switch to gunicorn (or similar) instead of runserver in init.py, except for environment=development. [UL-258]
* Pull additional google place info from some supported google takeout files (Reviews.json, and others?)

## Medium Features

## Larger Features
* Reduce duplicate code, remove legacy code, simplify codebase. [UL-30]
* Run bandit and AI vulnerability scans; integrate with CI/CD. [UL-31]

## Bug Fixes
* When a full pin refresh is occurring, navigating away from the page encounters latency. [UL-35]
* During import pins, checking "create badge", the badge is created, but the pins aren't added to it. (They are added to already existing badges you select, though) [UL-150]
* UI Bug: Multi-select toolbar in dark mode [UL-151]
* When changing a category to a tag, the tag is visually shown twice in the list until page refresh. [UL-152]
* Organize > Categories Tab -> Merge button is missing from rows. [UL-153]
* Organize > Categories Tab -> Edit / merge / delete button should be hidden when not in hover [UL-154]
* On pin details page, add badge dialog, the search bar doesn't work. [UL-195]
* Organize Page: Occasionally, after editing or merging badges, the edit button for other rows no longer opens the edit dialog. I'm not sure exactly what circumstances this happens. [UL-197]
* Badge Statuses can't be hierarchical?? (I guess they can, it just doesn't show in the organize status page ui) [UL-199]
* On pin details page, clicking edit, the dialog doesn't scroll when overflowing page height. [UL-215]
* Trip Details > Adding Pin: The suggestions are only geocoded, not pin searches. [UL-227]
* Starting map option: Remember doesn't appear to work. [UL-255]
* Organize: Bulk edit button doesn't open dialog.

## Map Search Filtering Polish
* The view options in the toolbar need a new button for "street details". 
---
* Changing badge icon / color in organize doesn't immediately trigger cache update.
---
* Throughout site: tooltips clip (overflow: hidden)
* Consider again: Pin count while filtering

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
* UI Bug in Dark Mode: Organize page -> Merge dialog doesn't show titles of cats being merged. [UL-191]
* Verify: Child trips work as expected. [UL-228]
* Password reset should go to application page, not django password reset page. [UL-256]
* Password reset should work elegantly with SSO users. [UL-257]
* Celery / async tasks: Move slow operations (API calls, geocoding, import jobs) to Celery tasks; all non-instant UI operations must show a progress indicator and use toast notifications on completion or failure [UL-119]
* In tools page, do only admins have access to the db backup tool? I guess we should probably move that to an admin-only page to be certain. (also, page width is wrong).

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
* Check for wikipedia entry for a pin, cache it, display data from it if it exists. I suppose this requires the wikipedia entry includes an address, which matches the pin, in order to avoid name collisions. [UL-64]
* Search wikimedia commons for images / assets for a pin / location. [UL-259]
* Gallery photos can have additional metadata, including: an angle of view, floor, room, etc. [UL-65]
* Types of friends: "connections", "friends", "close friends", etc? I'm not sure this is needed in light of people badges. However, the mobile app idea of "connect with explorer" would encourage adding someone as a connection without necessarily wanting them to be a friend. I suppose this is also useful in the web app if you regularly encounter someone you may want to DM or keep track of, but don't want them to be impacted by your privacy and sharing settings. [UL-66]
* Import data feature, so users can migrate from the publicly available app to their own private server if they wish. [UL-68]
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
* Organize Page: Create a new badge should probably open a dialog rather than an element inline. [UL-188]
* Create badge dialog -> We can put the "upload custom icon" in the choose icon dropdown, so it doesn't look like it's a separate thing. [UL-189]
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
* on client browser, const _PROFILE_ID = 1; is unnecessary and hints at vulnerabilities. [UL-208]
* Main map: add pin dialog -> tags and categories picker has them in 2 sections, instead of standardized picker with other badge kinds and a search. Icon section is empty (no options). No option to make it private. Overall: This dialog should reuse existing components instead of redefining the dialog features. [UL-210]
* BUG: Something I did caused a new pin to be created with a badge named "Unknown". My workflow started with the creation of a new pin by right-clicking on the main map. [UL-212]
* BUG: Developer toolbar only shows up when the ui admin setting for environment is set to development, but not when the env var is set to development and the ui setting is default. [UL-213]
* Pin details page: There should be a button to delete the pin. [UL-214]
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
* Replace "added by {username}" with nothing or "added by you" for the same user in order to reduce text and simplify the ui. [UL-232]
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
* Add site admin setting to make the site invite only, and suspend public signups. [UL-244]
* Loopnet API or Scraping [UL-248]
* Get pin / location bounding box from external service (i.e. property boundaries), or attempt ML building boundaries detection. [UL-249]
* Organize Page > Priority Tab: Provide mechanism for shifting an item to a specific position (e.g. "go to position 20"), and allow multi-select before dragging to drag as a group. [UL-250]
* Consider feature: on main map, the icon and the circle could be pulled from different places, allowing 2 pieces of information to be displayed about each pin. [UL-251]
* "Organize a meetup", which would encourage a larger audience, encourage invitees to invite friends, etc. To prevent abuse, possibly: meetup pin would only be shown to those who already had it, and invitees could vote on whether it was too vulnerable to share? Idk.

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
* Allow users to create and import pins without creating a community wiki entry. e.g. "My Girlfriend's House". [UL-106]
* Prevent users from "testing" if a location is abandoned by creating a test pin for it, then deleting said pin if no community wiki entry exists. Perhaps provide a delay before the community wiki entry is available to the user? Or cap pin creations? [UL-107]

## Code Quality
### Fix Generics
* tags = Badge.objects.tags() (and also .categories()) -> Cannot access attribute "categories" for class "Manager" [UL-126]
* profile = user.profile -> Cannot access attribute "profile" for class "User" [UL-127]

## From README Roadmap (migrated)
Items previously listed in README.md that are not already tracked elsewhere in this file. Some of these may already be implemented, so this list should be looked over and pruned before being relied on.

### Data
* Collect pin information during import.
* Remove (or better integrate) pin status (visited vs "visited" tag vs visit history).

### Community
* Ability to @ friends in comments, etc.
* User list (with privacy settings).
* Ability to view other user profiles by clicking on comments, etc.

### UI — General
* Allow user to reorder pin details sections.
* Change default pin details sections order.

### UI — Pin Detail Page
* Map sometimes double scrolls (latency?).
* Fix satellite view (street view may also be broken?).
* Fix web results (web results filtering through AI?).
* Fix boundary markup.
* Fix security indicators.
* Sections need stronger borders or colors.
* When adding tags: the search bar needs padding.

### UI — Trip Details Page
* Pin icons (1, 2, ...) should better communicate the idea, rather than looking like grouping blobs from other maps. Also should still use custom icons.
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
* Use aliases with Smithsonian, etc.
* Sunrise / sunset for weather.
* Address often incorrect Smithsonian results (AI filtering? Only names >= certain length?).

### Misc
* User settings: Allow friend requests checkbox is missing. Should have additional option for "from users with one pin in common", "1 mutual", etc.
* Clicking on notification should take you to the relevant page.
* Viewing notifications in the dropdown should mark them read. Not just clicking on them.
* Hide "(schedule) Never" in pin popup for last visited. This is already implied.