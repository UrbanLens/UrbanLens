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
    * User List (with privacy settings)
  * UI:
    * Dark mode
    * Mobile responsiveness
    * Trim icon options (currently too many)
    * Add missing icons (some themes aren't represented)
    * Allow user to reorder pin details sections
    * Change default pin details sections order
    * Better looking confirmation dialogs (delete, etc)
    * Pin detail page: 
      * Rating should be in stars
      * Place name - is this required to show?
      * Remove categories
      * Better show priority somehow
      * Scrolling the page shouldn't scroll the map (delay on map hover) 
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
      * Main detail section: Category is now out of date. Combine with description??
    * Trip Details Page:
      * <s>Map needs layers, etc.</s>
      * <s>Activities should be editable.</s>
      * Calendar view?
      * <s>After adding pins, map should auto update</s>
      * Pin icons (1, 2, ...) should better communicate the idea, rather than looking like grouping blobs from other maps. Also should still use custom icons.
      * <s>Activity view must show date proposal.</s>
      * <s>Add activity should allow adding an arbitrary address</s>
      * <s>Improve UI when pin search has no results.</s>
      * Delete should probably not delete for everyone?
      * Main Trip page: use the whitespace. Calendar? etc?
      * Allow archiving old events.
      * Notify other users when changes.
      * Allow users to drag/drop events to different dates.
      * Ensure users can see pins other users have added even if they don't have them ("Accept pins confirm"?)
      * Users can leave the trip.
      * Trip variations (confirmed/unconfirmed pins, map markup, etc)
      * RSVP per activity
    * Badges Page:
      * Custom Icons must be clamped to size (do this during upload?)
    * Map Page:
      * Icons are sometimes not showing up (caching? Cat icon for example.)
  * Development:
    * Unit Tests
    * CI/CD 
    * Code Coverage Report
  * Optimize:
    * Local storage in browser for faster loading and offline use
  * APIs
    * Use Aliases with Smithsonian, etc
    * Sunrise / Sunset for weather
    * Remove weather from pin details (should be only for trip details)
    * Address often incorrect Smithsonian results (AI filtering? Only names >= certain length?)