"""Start and manage the app without touching manage.py directly."""
# !/usr/bin/env python

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess  # nosec B404
import sys
from typing import Any

from djangofoundry import scripts
from djangofoundry.scripts import app
from packaging.requirements import InvalidRequirement, Requirement

from bin.utils.exceptions import DbStartError, UnsupportedCommandError
from bin.utils.settings import Settings

logger = Settings.get_logger(__name__)


class Actions(app.Actions):
    INSTALL = "install"


class App(scripts.App):
    """Extend djangofoundry app behaviour."""

    def get_argument(self, argument_name: str, args: tuple, kwargs: dict) -> Any:
        """Fetch an argument from args/kwargs.

        Args:
            argument_name: The name of the argument to retrieve.
            args: Positional arguments passed to the method.
            kwargs: Keyword arguments passed to the method.

        Returns:
            The argument value, if it exists. Otherwise, None.
        """
        # TODO: This duplicates new functionality from djangofoundry. When the package is updated to version 0.8, remove this method without any other changes.
        if len(args) == 1:
            return args[0]
        return kwargs.get(argument_name)

    def pip_install(self, package_name: str) -> bool:
        """Install a package via pip and pin it in requirements.txt.

        Args:
            package_name: The name of the package to install.

        Returns:
            True if installed successfully, False otherwise.

        Raises:
            ValueError: If package_name contains more than one package.
        """
        # TODO: This duplicates new functionality from djangofoundry. When the package is updated to version 0.8, remove this method without any other changes.

        try:
            Requirement(package_name)
        except InvalidRequirement as exc:
            raise ValueError(f'package_name must be a single valid Python requirement: "{package_name}"') from exc

        logger.info("Installing %s...", package_name)
        install_output = subprocess.check_output(  # nosec B603
            [sys.executable, "-m", "pip", "install", package_name],
            stderr=subprocess.STDOUT,
        ).decode("utf-8")

        if not (matches := re.search(r"Successfully installed (.*)", install_output)):
            return False

        if not (version := matches.group(1).split("-")[-1]):
            logger.warning("Could not determine version number for %s", package_name)
            return False

        if not re.match(r"^\d+\.\d+\.\d+$", version):
            logger.warning("Version number for %s is not valid: %s", package_name, version)
            return False

        logger.info("Adding %s to requirements.txt...", package_name)
        with Path("requirements.txt").open("a", encoding="utf-8") as f:
            f.write(f"{package_name}>={version}\n")

        return True

    def perform(self, command: Actions, *args, **kwargs) -> Any:
        """Perform the action for a command.

        Args:
            command: The action to perform.

        Returns:
            The result of the action.
        """
        self._command = command

        match command:
            case Actions.INSTALL:
                package_name = self.get_argument("package_name", args, kwargs)
                return self.pip_install(package_name)
            case _:
                return super().perform(command, *args, **kwargs)


def main():
    try:
        parser = argparse.ArgumentParser(description="Setup and manage the Django application (similar to manage.py).")
        parser.add_argument("action", choices=[e.value for e in Actions], help="The action to perform.")
        parser.add_argument("-p", "--project-name", default="myproject", help="The name of the project.")
        parser.add_argument("-a", "--author-name", help="The name of the author.")
        parser.add_argument("-d", "--directory", default=".", help="The directory for the project.")
        parser.add_argument(
            "-f",
            "--frontend-dir",
            default="frontend",
            help="The directory for the frontend (relative to -d).",
        )
        parser.add_argument(
            "-b",
            "--backend-dir",
            default="backend",
            help="The directory for the backend (relative to -d).",
        )
        parser.add_argument("-s", "--settings", default="conf/settings.yaml", help="The settings file to use.")
        parser.add_argument("--page-name", help="The name of the page to create.")
        parser.add_argument("--model-name", help="The name of the model to create.")
        parser.add_argument("--package-name", help="The name of the package to create.")

        options = parser.parse_args()

        try:
            settings = Settings(options.settings)

            app = App()
            app = App(
                options.project_name,
                options.author_name,
                settings,
                options.directory,
                options.frontend_dir,
                options.backend_dir,
            )

        except ValueError as ve:
            logger.exception("Bad option provided: %s", ve)
            sys.exit()

        except FileNotFoundError as fnf:
            logger.exception("Unable to find a necessary file: %s", fnf)
            sys.exit()

        try:
            command = Actions(options.action)
            result = app.perform(
                command,
                page_name=options.page_name,
                model_name=options.model_name,
                package_name=options.package_name,
            )

            if result is not None:
                logger.debug("App returned (%s)", result)
        except UnsupportedCommandError:
            logger.exception("Error: Unknown action. Try --help to see how to call this script.")
            sys.exit()

    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        sys.exit()
    except DbStartError:
        logger.exception("Could not start DB. Cannot continue")
        sys.exit()


if __name__ == "__main__":
    """Run when called directly."""
    main()
if __name__ == "__main__":
    main()
