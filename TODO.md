# Currently Planned Features
Features planned for this release.

## UI Adjustments
* Notification bar modernization (read/unread are not clear)
* Color tweaks throughout app for both light and dark mode.
* Tooltip UI
* Tweak notifications, and make success/error states more clear.
* Ensure mobile-first.
* Loading indicators for all ui actions that take time. (Creating pin, searching map, etc)
* "Don't leave page" dialog before a settings page is fully saved.

## Smaller Features
* Get user avatar during SSO account creation from the SSO provider, or gravatar.
* Discord SSO
* Add metadata for emojis (i.e. icons) to aid in searching for them.
* When creating maps for comments, allow using satellite mode or topographic mode as well as the default view.
* Cleanup git history, and begin using branches for dev.
* Properly set up pre-commit hooks for linting, type checking, and security scans.
* Include screenshots of the app in About page, and in the README.md file.
* Add tooltips to help guide users through the app when my assumptions about what is intuitive are incorrect.
* Configure SMTP for outgoing emails.
* Adjust "mark as completed" on trip planning page to show confirmation dialog, and be a little less easy for users to mess up. To make it more clear, the confirmation dialog can allow user to specify a date in the past, which defaults to today.

## Medium Features
* Share button on pin details page to share with a specific friend.
* "Accept" / "Reject" shared pin for the user being shared with.
* Support partial cache updates, instead of refreshing the cache for all pins at once.
* Cache should possibly (maybe?) include some metadata, so that searching on the map is faster.
* In addition to the above item, there are probably optimizations to be made with the DB to make server-side searching faster.
* Proper CI/CD pipeline, tags, releases, etc.
* When creating the community wiki entry for a pin, ensure we're not leaking user data to it that the user expects to be private. For instance, the community wiki entry should probably be titled based on the google place name, not the user's custom title. Perhaps we can offer a choice between the two when the user is creating only a single pin?
* Implement "hide user", and "mute user" features, alongside the existing "block user" feature.
* Limit failed login attempts.

## Larger Features
* Discord Integration
* Reduce duplicate code, remove legacy code, simplify codebase.
* Run bandit and AI vulnerability scans; integrate with CI/CD.

## Bug Fixes
* Clicking outside of a dialog closes it, which is great. But clicking in the dialog and dragging outside unexpectedly closes it.
* When in the main map and the trip details page, drag/drop of a pin shouldn't be as easy at higher zoom levels. Not sure what I want here. Confirmation dialog? Disable at higher zoom?
* User settings don't seem to properly save.
* When a full pin refresh is occurring, navigating away from the page encounters latency.

## Optimizations / Latency
* Adding a pin to the map.
* Searching / Filtering the map.

## Project Health
* Setup JIRA board publicly [UL-2]
* Setup bug tracking (github issues?) [UL-1]
* Review AI-created unit tests. Eliminate useless ones to assist code coverage reports.
* Provide secondary safeguards for permissions.
* Ensure non-anonymized urls do not exist at all. Users should not be able to access urls we don't want them to access, (like .../profile/2/, instead of the uuid).

## Features that need verification
* password reset.

# Future Features
Features planned for future releases.

* Dark mode for the map
* Subscriptions (VIP, etc) allowing access to AI tools. This isn't necessary while it's just me, or friends, but will be necessary if non-friends join in order to reduce API costs.
* On the profile page, if the user has instagram linked, then show a section with the most recent instagram posts for that user.
* Allow users to specify their signal username. Ideally, make it easy for users to add them to their signal contacts if it's provided.
* On the pin details and location wiki pages, we want to be able to show the property ownership records. However, there is not a consistent database to access that information that I'm aware of. Therefore, we need to have a strategy for looking up that information per county. To accomplish this, we need to create code to use AI to determine where to find that information for the given county, attempt to access the record for the given location, and if that's successful, then save the strategy used to the DB so that the same strategy can be used for other addresses in that county in the future.
* Memcached (or similar) for users currently logged in.
* Visit logs can include photos or maps with markup, if the user desires.
* When user uploads a photo containing GPS data and a date, mark a visit log entry for that date, and attach the photo to it.
* Full explanation (user friendly) to setup the app
* Implement a report button for comments and other user content. I'm not certain how this should work given there cannot be a manual moderation system, by design, since the moderator would then be able to see pins they shouldn't otherwise be able to see. Some ideas: we could allow manual moderation but mask some details (including pin coordinates). We could allow manual moderation of comments and images in isolation without sharing pin details. We could implement a community-driven moderation system without giving access to anyone who can't already see the content.
* API cost tracking and reporting. We should keep track of estimated costs for each individual user, so that future reporting strategies can be implemented that have access to legacy data. Track "hours used", and page loads for estimated CPU load cost, and track API costs by their actual cost at time of use.
* Public "costs" reporting page for accountability, showing only combined costs for all users.
* Url slugs for users, pins, etc, so the urls are human readable. The drawback is that these urls for pins will be different for each user, and they may not understand that; I think that's ok (and that's still true in the current application state with uuids). Maybe urls with uuid should continue to work (maybe??? idk??) for the purpose of bookmarks. I'm really not sure of this last point.
* Messaging between users.
* Request browser notification permissions for users.
* Integrate gotify (?) for notifications to site admin.
* Allow users to vote on making a location "public", which would share it to all users (if those users wish). This requires substantial thought to get just right. The upside is that it would substantially encourage use of the app for new users, who haven't yet imported their own pins, by pre-populating their map with well known locations that are not vulnerable.
* "Get directions" button to send directions to their phone (or show on screen).
* AI suggestions on the trip planning page for when to schedule activities, taking into account drive time and user voting. AI suggestions of pins to add that are relevant to the trip. Etc.
* Trip planning page should have some ability to go to the pin details page (or location wiki) for each activity.
* Dark mode defaults to system setting.
* Better UI form fields (sliders, date pickers, etc).
* Check for wikipedia entry for a pin, cache it, display data from it if it exists. I suppose this requires the wikipedia entry includes an address, which matches the pin, in order to avoid name collisions.
* Gallery photos can have additional metadata, including: an angle of view, floor, room, etc.
* Types of friends: "connections", "friends", "close friends", etc? I'm not sure this is needed in light of people badges. However, the mobile app idea of "connect with explorer" would encourage adding someone as a connection without necessarily wanting them to be a friend. I suppose this is also useful in the web app if you regularly encounter someone you may want to DM or keep track of, but don't want them to be impacted by your privacy and sharing settings.
* Export data feature, allowing users to control and own their data as much as possible. The complexity here is "what format so this is at all useful?" Probably mimic google maps export formats, I guess?
* Import data feature, so users can migrate from the publicly available app to their own private server if they wish.
* Outside of app error logging. Alerts on certain kinds of errors.
* Address DDOS, spamming, etc.
* Saved map searches ("My Bucket List", etc).

## Really Big Ideas / Features
* Native android / ios apps (allowing expansion into additional features).
* Visualize a location, room, etc, by browsing similar photos chronologically in a visually stimulating way.
* Social media features (sharing content, stories, etc), allowing users to share content they want other explorers to see, but don't want to be publicly available on the internet to a non-exploring audience.
* Buffer features (maybe using their api?) for buffer-like functionality that's tailored toward exploring workflows. (This may be straying too much from the core app purpose)
* Look into decentralized stuff.
* Sync with some other service. (I don't think google maps is possible - see the "issues I don't think are solvable" section - but other services may be possible). This would provide a portion of a backup strategy for user data
* Kubernetes
* More API access for finding vintage photos and documents, location details, alerts, etc.
* Reduce reliance on javascript further by migrating more of it to HTMX.
* "Demolition Alert" feature. I'm not sure if this is practically possible, since regularly searching for every pin is out of the question.

### Native Mobile App
* Automatically check off visit logs
* "Who is here?" ping feature, allowing other users with the app to opt in to sharing their location. This solves the "I hear footsteps" problem.
* Track trip progress via gps, device motion, etc. This allows the user to remember what route they took, and could help address mapping tunnels.
* "I've been hurt" features, allowing users to keep their location secret unless they hit a button, or don't get back on the app after an explore, at which point their last known location and trip details are sent to emergency contacts.
* "Emergency device lock" feature, similar to an app from the ACLU, which turns on recording, disables notifications on the homescreen, disables fingerprint and face unlock, etc.
* "Location Warning" feature, allowing users to set a warning radius around their location, and other users in that radius can be notified (if they wish).
* "People on site group chat".
* Trip participants 'share my location'" feature to regroup after you split up. (opt-in)
* "Take and immediately upload" photo feature for trips, (or ?maybe? for community locations). This allows group trips to tell the other participants: "come over to this room to see this thing" quickly.
* Integrate minor photopills features.
* "Exploring Mode" changes notification sounds to subtle, ambient sounds.
* "Connect with explorer" feature when encountering someone new. Could also support connecting with non-app users somehow, or at least creating a note about the connection.

#### Crazy Stuff
This could be a playground for implementing a few exploratory ideas I've had in my head for a while.
* Person scanning via wifi
* Connect to other friendly devices (mobile ip camera, etc)
* Detect cameras, sensors nearby.
* Scan emergency frequencies to notify of issues.
* Notification for "exit time before sundown" or similar?

## Ideas to Consider
* Link to (or pull more data from) google maps, openstreetmap, mapquest, etc.
* Keep track of "encountered" users when using the app. This allows display of a fun stat: "first encountered", allows looking up people you've seen before but didn't connect with, and encourages social interaction. This would also facilitate restricting access to a user's profile unless they have been "encountered" by the current user (i.e. the user could not just type in a url with the user's slug, or be given a url with their uuid. Instead, they'd have to invite a connection with the user first, by email address, and allow the other user to opt in to the interaction.)
* Consider adding privacy controls to explicitly hide content from certain types of users, which would override the whitelist privacy controls the user set. For instance: "Show pins to users with 1 trip in common" and "hide pins from users with a specific badge" would give more control over privacy and sharing. I'm not sure how to do this in a way where the UI isn't overly complex and clunky. (maybe "advanced privacy controls"?)

## Issues I don't think are solvable
* Encrypting user data so the site admin doesn't have access to it. The only two solutions I can think of are (1) a peer-to-peer sharing system, or (2) separating the app into a "server" and "agent" app, wherein the client app has unencrypted data, but the server only has encrypted data. For (2), users would then be able to set up their own "agent" app on their own server, resulting in full ownership of their data. However, both solutions suffer from significant drawbacks. The latter is more attainable, but in order for the app to be usable for most users, we need a publicly hosted client app anyway, resulting in no privacy gains for most (or possibly for any) users. In addition, both solutions suffer significant performance penalties, and technical complexity, for little to no gain. Finally, almost no users will understand the key differences between this problem being solved and not being solved, and will assume that data is unencrypted and visible to the site admin even if it is not. Therefore, I'm not certain that implementing it really improves user trust, while nonetheless encountering additional drawbacks. The main reason to do it seems to be to tell users we did it... which seems less beneficial than its cost. I'm undecided on this.
* Considerations about avoiding storing identifying user data. Given SSO, and a need to email the user, I'm not certain that this is solvable. 1-way hashing combined with a "verify your email before..." dialog could help address it, but that would only allow us to hash the email, not avoid storing it altogether, which would still make it crackable via brute force. In addition, it would interfere with our ability to email notifications. Users can give themselves full anonymity already by registering a new email address and choosing not to provide SSO or personal details during account creation. Providing those kinds of instructions might be helpful somewhere, and we could possibly provide a button on the profile page to allow them to anonymize their existing account in that way if they originally created their account the "wrong" way and want full anonymity going forward.
* Sync with google maps. Google maps does not allow labelling pins, or adding them to lists via a programmatic interface, and the only way to export data is through the google takeout system. The only way to mimic this would be through web scraping, which would be extremely fragile, and require users to grant way too many permissions to our app. Theoretically, this limitation could change in the future, depending entirely on google.

## Issues requiring architectural solutions
* Allow users to interact with parts of the app (by invite?) without logging in. For instance, in the case of trip planning.
* Allow users to create and import pins without creating a community wiki entry. e.g. "My Girlfriend's House".
* Prevent users from "testing" if a location is abandoned by creating a test pin for it, then deleting said pin if no community wiki entry exists. Perhaps provide a delay before the community wiki entry is available to the user? Or cap pin creations?