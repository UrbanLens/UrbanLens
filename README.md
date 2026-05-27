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
    * Trip Details Page:
      * Map needs layers, etc.
      * Activities should be editable.
      * Calendar view?
      * After adding pins, map should auto update
      * Pin icons (1, 2, ...) should better communicate the idea, rather than looking like grouping blobs from other maps. Also should still use custom icons.
      * Activity view must show date proposal.
      * Add activity should allow adding an arbitrary address
      * Improve UI when pin search has no results.
      * Delete should probably not delete for everyone?
      * Main Trip page: use the whitespace. Calendar? etc?
      * Allow archiving old events.
      * Notify other users when changes.
  * Development:
    * Unit Tests
    * CI/CD 
    * Code Coverage Report
  * APIs
    * Use Aliases with Smithsonian, etc
    * Sunrise / Sunset for weather
    * Remove weather from pin details (should be only for trip details)
    * Address often incorrect Smithsonian results (AI filtering? Only names >= certain length?)