## About
UrbanLens is a web-based mapping application designed for photographers and urban explorers, focusing on organizing and sharing unique urban locations responsibly. The main features include User Authentication, Personalized Map Interface, Location Pinning, Location Details and Notes, Social Integration, and Trip Planning Tools. The code leverages the power of several other technologies including Ruff for Python static analysis, HTMX for interactivity, and pre-commit for managing pre-commit hooks. 

## Project Structure
The project is structured following a standard Django project layout, and follows the google style guide for python.

## Setup
To set up the project, follow these steps:
1. Clone the repository.
2. Install the required packages using `pip install -r requirements.txt`.
3. Set up the database by running `python manage.py migrate`.
4. Collect static files by running `python manage.py collectstatic`.

## Running the Project
To run the project, use the command `python manage.py runserver`. This will start the Django development server on your local machine.

## Contributing
Contributions to this project are welcome. Please fork the repository, make your changes, and submit a pull request. Ensure that your code adheres to the existing style conventions and passes all tests. All PRs should come with unit tests to demonstrate functionality.

## TODO
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