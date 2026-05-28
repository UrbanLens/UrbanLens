![Urban Lens Logo](/dashboard/frontend/static/dashboard/images/logo_bw.png)

## About
UrbanLens is a web-based mapping application designed for photographers and urban explorers, focusing on organizing and sharing urbex locations responsibly. Features include User Authentication, a personalized Map interface, location pinning, notes, social integration, and Trip planning tools. The code leverages the power of several other technologies including Ruff for Python static analysis, HTMX for interactivity, and pre-commit for managing pre-commit hooks. 

## Project Structure
The project is structured following a standard Django project layout, and follows the google style guide for python.

## Running the Project
The easiest way to run the project is with docker:
1. Clone the repository.
2. Copy .env-sample to .env, and fill in your details
3. Run the project with: docker-compose up --build

## Contributing
Contributions to this project are welcome. Ensure that your code adheres to the existing style conventions and passes all tests. Please submit pull requests along with unit tests to demonstrate functionality.

## Roadmap
* Map:
    * <s>Map layers</s>
    * <s>Change pin icons</s>
    * <s>Allow using emojis as pin icons from a standard emoji pack</s>
    * <s>Allow uploading custom images for pin icons</s>
* Search:
    * <s>Search functionality</s>
* Research:
    * <s>Location view page</s>
    * <s>Weather</s>
    * <s>Satelite View</s>
    * <s>Street View</s>
    * <s>Historic photos</s>
    * <s>Web Search</s>
* Integrations:
    * <s>Google SSO</s>
* Data:
    * <s>Google maps import</s>
    * Collect pin information during import
    * Export
    * Remove (or better integrate) pin status (visited vs "visited" tag vs visit history)
* Planning:
    * <s>Trip page</s>
* Community:
    * Messages between users
    * <s>Friends</s>
    * Sharing
    * Comments
      * Ability to @ friends, etc
    * User List (with privacy settings)
    * Ability to view other user profiles by clicking on comments, etc
* UI:
  * <s>Dark mode</s>
  * Mobile responsiveness
  * Trim icon options (currently too many)
  * Add missing icons (some themes aren't represented)
  * Allow user to reorder pin details sections
  * Change default pin details sections order
  * Better looking confirmation dialogs (delete, etc)
  * Beautify registration / login pages
    * Standardize SSO registration buttons
    * After login, we are redirected to accounts/profile, which is a 404, instead of dashboard/profile.
  * Pin detail page: 
    * <s>Rating should be in stars</s>
    * <s>Place name - is this required to show?</s>
    * <s>Remove categories</s>
    * <s>Better show priority somehow</s>
    * <s>Scrolling the page shouldn't scroll the map (delay on map hover)</s> 
    * Map sometimes double scrolls (latency?)
    * Fix Sat view (street may also be broken?)
    * Fix web results
      * Web results filtering through ai?
    * Fix boundary markup
    * Fix security indicators
    * Map search needs overhaul
    * Sections need stronger borders or colors
    * Users can reorder sections
    * When adding tags: 
      * the search bar needs padding.
      * Clicking outside the dialog should close it.
    * <s>Main detail section: Category is now out of date. Combine with description??</s>
  * Trip Details Page:
    * <s>Map needs layers, etc.</s>
    * <s>Activities should be editable.</s>
    * <s>Calendar view?</s>
    * <s>After adding pins, map should auto update</s>
    * Pin icons (1, 2, ...) should better communicate the idea, rather than looking like grouping blobs from other maps. Also should still use custom icons.
    * <s>Activity view must show date proposal.</s>
    * <s>Add activity should allow adding an arbitrary address</s>
    * <s>Improve UI when pin search has no results.</s>
    * Delete should probably not delete for everyone?
    * Main Trip page: use the whitespace. Calendar? etc?
    * Allow archiving old events.
    * Notify other users when changes.
    * <s>Allow users to drag/drop events to different dates.</s>
    * Ensure users can see pins other users have added even if they don't have them ("Accept pins confirm"?)
    * <s>Users can leave the trip.</s>
    * Trip variations (<s>confirmed/unconfirmed pins</s>, map markup, variation 1/2/3, etc)
    * RSVP per activity
    * Leaving a trip should take you back to the trip list.
    * Users can click on the map to add a pin
    * Ability to drag and drop some pins on the map (especially ones that were added via coordinates or right clicking)
    * Ability to add pins based on coordinate, not just geolookup addresses.
    * ...and based on places lookup, maybe?
    * Order activity list by date
    * Add one trip inside another??
    * Multiple organizers
    * Map bug (grey tile on right side)
    * Activity end dates
    * Trip settings - fix checkbox bug. Also, each option should have 3 states (no one, organizers, everyone)
    * In activity edit dialog, add delete button
    * Add some additional descriptor for activities (an icon, or a category? For instance: Camping, Food)
    * Never say "0 minutes ago"
    * When comment has image, must be indication that image will be uploaded after it's selected from user's computer.
    * Reply button beneath replies
    * Bug: Comment count does not count replies
    * Bug: After deleting comment, the comment section duplicates itself.
  * Badges Page:
    * <s>Custom Icons must be clamped to size (do this during upload?)</s>
  * Map Page:
    * <s>Icons are sometimes not showing up (caching? Cat icon for example.)</s>
* Development:
  * Unit Tests
  * CI/CD 
  * Code Coverage Report
* Optimize:
  * Local storage in browser for faster loading and offline use
* APIs
  * Use Aliases with Smithsonian, etc
  * Sunrise / Sunset for weather
  * <s>Remove weather from pin details (should be only for trip details)</s>
  * Address often incorrect Smithsonian results (AI filtering? Only names >= certain length?)
* Misc:
  * User settings: Allow friend requests checkbox is missing. Should have additional option for "from users with one pin in common", "1 mutual", etc.
  * Privacy setting for who can find you by search, or by your email, or by using your handle directly.
  * Clicking on notification should take you to the relevant page.
  * Viewing notifications in the dropdown should mark them read. Not just clicking on them.
  * Notification log makes read messages look unread. UI with this needs cleanup.
  * Hide "(schedule) Never" in pin popup for last visited. This is already implied.
  * When selecting tags to edit, enable shift+click
  * Bug: Once you've edited one tag, the edit dialog doesn't come up again for other clicks of the edit button.
  * Optimize: The organize page takes a long time to load.