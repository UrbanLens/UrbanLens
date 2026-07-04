![Urban Lens Logo](/dashboard/frontend/static/dashboard/images/logo_bw.png)

[![CI](https://github.com/UrbanLens/UrbanLens/actions/workflows/ci.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/ci.yml) [![Security](https://github.com/UrbanLens/UrbanLens/actions/workflows/security.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/security.yml) [![Release Please](https://github.com/UrbanLens/UrbanLens/actions/workflows/release-please.yml/badge.svg)](https://github.com/UrbanLens/UrbanLens/actions/workflows/release-please.yml)

## About
UrbanLens is a web-based mapping application designed for photographers and urban explorers, focusing on organizing and sharing urbex locations responsibly. Features include User Authentication, a personalized Map interface, location pinning, notes, social integration, and Trip planning tools. The code leverages the power of several other technologies including Ruff for Python static analysis, HTMX for interactivity, and pre-commit for managing pre-commit hooks. 

## Project Structure
The project is structured following a standard Django project layout, and follows the google style guide for python.

## Running the Project
The easiest way to run the project is with docker:
1. Clone the repository.
2. Copy .env-sample to .env, and fill in your details
3. Run the project with: docker-compose up --build


## CI/CD and Releases

UrbanLens uses GitHub Actions for automated quality gates, security checks, release preparation, and artifact publishing:

* Pull requests and pushes to `main` run Python linting, formatting checks, type checks, Django checks, tests with coverage, frontend asset builds, and a Docker image build.
* Security automation runs CodeQL, dependency review for pull requests, and scheduled weekly scans.
* Dependabot opens weekly updates for GitHub Actions, Python, npm, and Docker dependencies.
* Releases are managed with Release Please. Use Conventional Commit messages such as `feat: add route export` or `fix: handle empty weather data`; merges to `main` update a release PR, and merging that PR creates the version tag, changelog, and GitHub release.
* Published GitHub releases build Python distributions and a provenance-attested container image in GitHub Container Registry (`ghcr.io/UrbanLens/UrbanLens`) tagged with the semantic version and `latest` for stable releases.

## Contributing
Contributions to this project are welcome. Ensure that your code adheres to the existing style conventions and passes all tests. Please submit pull requests along with unit tests to demonstrate functionality.