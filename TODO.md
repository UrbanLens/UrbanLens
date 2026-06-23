# Currently Planned Features
Features planned for this release.

## UI Adjustments
* Notification bar modernization (read/unread are not clear) [UL-3]
* Color tweaks throughout app for both light and dark mode. [UL-4]
* Tooltip UI [UL-5]
* Tweak notifications, and make success/error states more clear. [UL-6]
* Ensure mobile-first. [UL-7]
* Loading indicators for all ui actions that take time. (Creating pin, searching map, etc) [UL-8]

## Smaller Features
* Cleanup git history, and begin using branches for dev. [UL-14]
* Include screenshots of the app in About page, and in the README.md file. [UL-16]
* Add tooltips to help guide users through the app when my assumptions about what is intuitive are incorrect. [UL-17]

## Medium Features

## Larger Features
* Reduce duplicate code, remove legacy code, simplify codebase. [UL-30]
* Run bandit and AI vulnerability scans; integrate with CI/CD. [UL-31]

## Bug Fixes
* User settings don't seem to properly save. [UL-34]
* When a full pin refresh is occurring, navigating away from the page encounters latency. [UL-35]

## Optimizations / Latency
* Adding a pin to the map. [UL-36]
* Searching / Filtering the map. [UL-37]
* Cache API results, like Street View and Satellite View images. [UL-113]

## Project Health
* Review AI-created unit tests. Eliminate useless ones to assist code coverage reports. [UL-38]
* Provide secondary safeguards for permissions. [UL-39]
* Prune unnecessary vars from docker-compose, .env, etc

## Features that need verification
* password reset. [UL-41]
* Verify Feature: Possible issue with then pulling or displaying visit history entries. [UL-114]
* Verify Feature: On the pin details page, if the smithsonian archive section is empty, then hide it. [UL-115]
* Verify Feature: On the pin details page, there is a notes section and a comments section. But only one is needed. Keep comments, but remove the notes. Attempt to display a street address for the pin, assuming we can figure out what that address would be, and make sure that address is cached so we don't have to contact an external api multiple times. [UL-116]
* Verify Feature: When performing google or brave searches, add the street name, city, and state to the search query as optional keywords, to help disambiguate with unrelated results. [UL-117]
* Support partial cache updates, instead of refreshing the cache for all pins at once. [UL-22]
* Cache should possibly (maybe?) include some metadata, so that searching on the map is faster. [UL-23]
* In addition to the above item, there are probably optimizations to be made with the DB to make server-side searching faster. [UL-24]
* When clearing data, or uninstalling and redeploying the app, a user's pins will not exist in the server-side db, but they will still exist in the local browser cache. As a result, when the user loads the map page, pins are shown that don't exist. In this case, the cache should be cleared. We can simplify that process by creating an app uuid when the app is first deployed, and include that uuid in the local cache. The cache should also only apply to a given user, and that user should be specified by uuid, not the PK id, so hackers cannot see the PK user id on the client side. [UL-122]
* Limit failed login attempts. [UL-28]
* Add metadata for emojis (i.e. icons) to aid in searching for them. [UL-12]
* When creating maps for comments, allow using satellite mode or topographic mode as well as the default view. [UL-13]
* Discord SSO [UL-11]
* "Don't leave page" dialog before a settings page is fully saved. [UL-9]
* Clicking outside of a dialog closes it, which is great. But clicking in the dialog and dragging outside unexpectedly closes it. [UL-32]
* On the public profile page, when saving a note, the note section is duplicated. [UL-112]
* When in the main map and the trip details page, drag/drop of a pin shouldn't be as easy at higher zoom levels. Not sure what I want here. Confirmation dialog? Disable at higher zoom? [UL-33]
* Allow users to specify their username in account creation, and pick an avatar. Defaults will be the options we currently set without their input. [UL-125]
* We must allow pins to be marked by a user as 'private', in which case they do not create a location wiki entry. [UL-110]
* During import pins, consider mapping badges, or other fine-grained control of the import process. (I'm not sure what's needed here... but it's an area to consider improvements) [UL-111]
* Configure SMTP for outgoing emails. [UL-18]
* When creating the community wiki entry for a pin, ensure we're not leaking user data to it that the user expects to be private. For instance, the community wiki entry should probably be titled based on the google place name, not the user's custom title. Perhaps we can offer a choice between the two when the user is creating only a single pin? [UL-26]
* Adjust "mark as completed" on trip planning page to show confirmation dialog, and be a little less easy for users to mess up. To make it more clear, the confirmation dialog can allow user to specify a date in the past, which defaults to today. [UL-19]
* After converting a badge type, then switching tabs, the converted badge doesn't appear in the expected list. [UL-123]
* The pre-populated People badges do not appear on the organize page under the People tab. [UL-124]
* Ensure non-anonymized urls do not exist at all. Users should not be able to access urls we don't want them to access, (like .../profile/2/, instead of the uuid). [UL-40]
* Properly set up pre-commit hooks for linting, type checking, and security scans. [UL-15]
* Password Requirements should be reasonably strong.

# Future Features
Features planned for future releases.

* Dark mode for the map [UL-42]
* Subscriptions (VIP, etc) allowing access to AI tools. This isn't necessary while it's just me, or friends, but will be necessary if non-friends join in order to reduce API costs. [UL-43]
* On the profile page, if the user has instagram linked, then show a section with the most recent instagram posts for that user. [UL-44]
* Allow users to specify their signal username. Ideally, make it easy for users to add them to their signal contacts if it's provided. [UL-45]
* On the pin details and location wiki pages, we want to be able to show the property ownership records. However, there is not a consistent database to access that information that I'm aware of. Therefore, we need to have a strategy for looking up that information per county. To accomplish this, we need to create code to use AI to determine where to find that information for the given county, attempt to access the record for the given location, and if that's successful, then save the strategy used to the DB so that the same strategy can be used for other addresses in that county in the future. [UL-46]
* Memcached (or similar) for users currently logged in. [UL-47]
* Visit logs can include photos or maps with markup, if the user desires. [UL-48]
* When user uploads a photo containing GPS data and a date, mark a visit log entry for that date, and attach the photo to it. [UL-49]
* Full explanation (user friendly) to setup the app [UL-50]
* Implement a report button for comments and other user content. I'm not certain how this should work given there cannot be a manual moderation system, by design, since the moderator would then be able to see pins they shouldn't otherwise be able to see. Some ideas: we could allow manual moderation but mask some details (including pin coordinates). We could allow manual moderation of comments and images in isolation without sharing pin details. We could implement a community-driven moderation system without giving access to anyone who can't already see the content. [UL-51]
* API cost tracking and reporting. We should keep track of estimated costs for each individual user, so that future reporting strategies can be implemented that have access to legacy data. Track "hours used", and page loads for estimated CPU load cost, and track API costs by their actual cost at time of use. [UL-52]
* Public "costs" reporting page for accountability, showing only combined costs for all users. [UL-53]
* Url slugs for users, pins, etc, so the urls are human readable. The drawback is that these urls for pins will be different for each user, and they may not understand that; I think that's ok (and that's still true in the current application state with uuids). Maybe urls with uuid should continue to work (maybe??? idk??) for the purpose of bookmarks. I'm really not sure of this last point. [UL-54]
* Messaging between users. [UL-55]
* Request browser notification permissions for users. [UL-56]
* Integrate gotify (?) for notifications to site admin. [UL-57]
* Allow users to vote on making a location "public", which would share it to all users (if those users wish). This requires substantial thought to get just right. The upside is that it would substantially encourage use of the app for new users, who haven't yet imported their own pins, by pre-populating their map with well known locations that are not vulnerable. [UL-58]
* "Get directions" button to send directions to their phone (or show on screen). [UL-59]
* AI suggestions on the trip planning page for when to schedule activities, taking into account drive time and user voting. AI suggestions of pins to add that are relevant to the trip. Etc. [UL-60]
* Trip planning page should have some ability to go to the pin details page (or location wiki) for each activity. [UL-61]
* Dark mode defaults to system setting. [UL-62]
* Better UI form fields (sliders, date pickers, etc). [UL-63]
* Check for wikipedia entry for a pin, cache it, display data from it if it exists. I suppose this requires the wikipedia entry includes an address, which matches the pin, in order to avoid name collisions. [UL-64]
* Gallery photos can have additional metadata, including: an angle of view, floor, room, etc. [UL-65]
* Types of friends: "connections", "friends", "close friends", etc? I'm not sure this is needed in light of people badges. However, the mobile app idea of "connect with explorer" would encourage adding someone as a connection without necessarily wanting them to be a friend. I suppose this is also useful in the web app if you regularly encounter someone you may want to DM or keep track of, but don't want them to be impacted by your privacy and sharing settings. [UL-66]
* Export data feature, allowing users to control and own their data as much as possible. The complexity here is "what format so this is at all useful?" Probably mimic google maps export formats, I guess? [UL-67]
* Import data feature, so users can migrate from the publicly available app to their own private server if they wish. [UL-68]
* Outside of app error logging. Alerts on certain kinds of errors. [UL-69]
* Address DDOS, spamming, etc. [UL-70]
* Saved map searches ("My Bucket List", etc). [UL-71]
* Allow importing of timeline data to mark pins as Visited [UL-118]
* Celery / async tasks: Move slow operations (API calls, geocoding, import jobs) to Celery tasks; all non-instant UI operations must show a progress indicator and use toast notifications on completion or failure [UL-119]
* Hypothesis unit tests: Add property-based tests wherever possible. [UL-120]
* App setup page on first run: configure site name, etc. [UL-121]
* Discord Integration [UL-29]
* Setup bug tracking (github issues?) [UL-1]
* Share button on pin details page to share with a specific friend. [UL-20]
* "Accept" / "Reject" shared pin for the user being shared with. [UL-21]
* Implement "hide user", and "mute user" features, alongside the existing "block user" feature. [UL-27]
* Proper CI/CD pipeline, tags, releases, etc. [UL-25]

## Really Big Ideas / Features
* Native android / ios apps (allowing expansion into additional features). [UL-72]
* Visualize a location, room, etc, by browsing similar photos chronologically in a visually stimulating way. [UL-73]
* Social media features (sharing content, stories, etc), allowing users to share content they want other explorers to see, but don't want to be publicly available on the internet to a non-exploring audience. [UL-74]
* Buffer features (maybe using their api?) for buffer-like functionality that's tailored toward exploring workflows. (This may be straying too much from the core app purpose) [UL-75]
* Look into decentralized stuff. [UL-76]
* Sync with some other service. (I don't think google maps is possible - see the "issues I don't think are solvable" section - but other services may be possible). This would provide a portion of a backup strategy for user data [UL-77]
* Kubernetes [UL-78]
* More API access for finding vintage photos and documents, location details, alerts, etc. [UL-79]
* Reduce reliance on javascript further by migrating more of it to HTMX. [UL-80]
* "Demolition Alert" feature. I'm not sure if this is practically possible, since regularly searching for every pin is out of the question. [UL-81]

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
* Encrypting user data so the site admin doesn't have access to it. The only two solutions I can think of are (1) a peer-to-peer sharing system, or (2) separating the app into a "server" and "agent" app, wherein the client app has unencrypted data, but the server only has encrypted data. For (2), users would then be able to set up their own "agent" app on their own server, resulting in full ownership of their data. However, both solutions suffer from significant drawbacks. The latter is more attainable, but in order for the app to be usable for most users, we need a publicly hosted client app anyway, resulting in no privacy gains for most (or possibly for any) users. In addition, both solutions suffer significant performance penalties, and technical complexity, for little to no gain. Finally, almost no users will understand the key differences between this problem being solved and not being solved, and will assume that data is unencrypted and visible to the site admin even if it is not. Therefore, I'm not certain that implementing it really improves user trust, while nonetheless encountering additional drawbacks. The main reason to do it seems to be to tell users we did it... which seems less beneficial than its cost. I'm undecided on this. [UL-102]
* Considerations about avoiding storing identifying user data. Given SSO, and a need to email the user, I'm not certain that this is solvable. 1-way hashing combined with a "verify your email before..." dialog could help address it, but that would only allow us to hash the email, not avoid storing it altogether, which would still make it crackable via brute force. In addition, it would interfere with our ability to email notifications. Users can give themselves full anonymity already by registering a new email address and choosing not to provide SSO or personal details during account creation. Providing those kinds of instructions might be helpful somewhere, and we could possibly provide a button on the profile page to allow them to anonymize their existing account in that way if they originally created their account the "wrong" way and want full anonymity going forward. [UL-103]
* Sync with google maps. Google maps does not allow labelling pins, or adding them to lists via a programmatic interface, and the only way to export data is through the google takeout system. The only way to mimic this would be through web scraping, which would be extremely fragile, and require users to grant way too many permissions to our app. Theoretically, this limitation could change in the future, depending entirely on google. [UL-104]
* Support non-USA formats for dates, currency, distances via user settings.
* Support non-English language.
* User stats page (fun stats about the user: breakdown of pins by continent, etc).
* Lists (these aren't strictly necessary, due to badges, but could allow users to create lists of unrelated things. Like "my favorite explores in February" or "1 Best Church in Each State").

## Issues requiring architectural solutions
* Allow users to interact with parts of the app (by invite?) without logging in. For instance, in the case of trip planning. [UL-105]
* Allow users to create and import pins without creating a community wiki entry. e.g. "My Girlfriend's House". [UL-106]
* Prevent users from "testing" if a location is abandoned by creating a test pin for it, then deleting said pin if no community wiki entry exists. Perhaps provide a delay before the community wiki entry is available to the user? Or cap pin creations? [UL-107]

## Code Quality
### Fix Generics
* tags = Badge.objects.tags() (and also .categories()) -> Cannot access attribute "categories" for class "Manager" [UL-126]
* profile = user.profile -> Cannot access attribute "profile" for class "User" [UL-127]

## Inconsistent Behavior
Behavior that isn't consistent enough to diagnose or fix without looking at it more deeply.

* Sometimes... In the import dialog ui, hovering over the drop files here or browse button has a css hover effect that makes it look clickable, but clicking on it doesn't do anything. The user has to click on the browse link. Clicking anywhere on the button should work just as if the user clicked on the browse link.