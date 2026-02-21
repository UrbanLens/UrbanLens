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
    * Allow using emojis as pin icons from a standard emoji pack
    * Allow uploading custom images for pin icons
* Search:
    * Search functionality
* Research:
    * <s>Location view page</s>
    * <s>Weather</s>
    * <s>Satelite View</s>
    * <s>Street View</s>
    * Aerial view
    * <s>Historic photos</s>
    * <s>Web Search</s>
    * Comments
* Integrations:
    * Discord
    * <s>Google SSO</s>
* Data:
    * <s>Google maps import</s>
    * Collect pin information during import
    * Export
* Planning:
    * Trip page
* Community:
    * Messages between users
    * Friends
    * Sharing