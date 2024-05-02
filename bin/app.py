"""*********************************************************************************************************************
*                                                                                                                      *
*
    This script should start up our app and manage it, without having to interact with django's manage.py script.

    This allows us to abstract django away, while also giving us additional tools specific to our project.
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    app.py                                                                                               *
*        Path:    /bin/app.py                                                                                          *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

#!/usr/bin/env python

# Generic imports
from __future__ import annotations
import argparse
import re
import sys
import subprocess
from typing import Any
from djangofoundry import scripts
from djangofoundry.scripts import app
# Our imports
from utils.exceptions import DbStartError, UnsupportedCommandError
from utils.settings import Settings

logger = Settings.getLogger(__name__)

class Actions(app.Actions):
    # Define additional actions
    INSTALL = 'install'

class App(scripts.App):
    """
    This functionality is implemented within djangofoundry.

    We are extending it here to add additional functionality that is custom to our app (todo).
    """

    def get_argument(self, argument_name : str, args : tuple, kwargs : dict ) -> Any:
        """
        Retrieves an argument from args/kwargs.

        This is useful for methods like self.perform() where we want to pass arguments to an arbitrary method, which may be different per command.

        Args:
            argument_name (str):
                The name of the argument to retrieve
            args (tuple):
                The list of arguments passed to the method
            kwargs (dict):
                The dictionary of keyword arguments passed to the method

        Returns:
            The argument value, if it exists. Otherwise, None.

        Examples:
            >>> class Foo(App):
            >>> 	def change_page(self, *args, **kwargs):
            >>> 		argument = self.get_argument('page_name', args, kwargs)
            >>> 		print('page_name = ' + argument)
            >>> foo = Foo()
            >>> foo.change_page('home')
            page_name = home
        """
        # TODO: This duplicates new functionality from djangofoundry. When the package is updated to version 0.8, remove this method without any other changes.
        if len(args) == 1:
            return args[0]
        else:
            return kwargs.get(argument_name, None)

    def pip_install(self, pip_package : str) -> bool:
        """
        Install a python package using pip, and add it (with version) to requirements.txt.

        Args:
            pip_package (str): The name of the package to install.

        Returns:
            bool: True if the package was installed successfully, False otherwise.

        Raises:
            ValueError: If pip_package contains more than one package.

        Examples:
            >>> app = App()
            >>> app.pip_install('requests')
            True

            >>> app.pip_install('requests==2.26.0')
            True

            >>> app.pip_install('requests==2.26.0 git')
            Traceback (most recent call last):
                ...
        """
        # TODO: This duplicates new functionality from djangofoundry. When the package is updated to version 0.8, remove this method without any other changes.

        # Ensure that pip_package is only 1 package
        if len(pip_package.split(' ')) > 1:
            raise ValueError(f'pip_package must be a single package. "{pip_package}" contains more than one package.')

        # Install the package, capture output so that we can determine the version number of the package
        logger.info(f'Installing {pip_package}...')
        install_output = subprocess.check_output([sys.executable, '-m', 'pip', 'install', pip_package], stderr=subprocess.STDOUT).decode('utf-8')

        # Grab the version number from the output
        if not (matches := re.search(r'Successfully installed (.*)', install_output)):
            return False

        if not (version := matches.group(1).split('-')[-1]):
            logger.warning(f'Could not determine version number for {pip_package}')
            return False

        # Ensure version is a valid version number
        if not re.match(r'^\d+\.\d+\.\d+$', version):
            logger.warning(f'Version number for {pip_package} is not valid: {version}')
            return False

        # Add package (and version #) to requirements.txt.
        logger.info(f'Adding {pip_package} to requirements.txt...')
        with open('requirements.txt', 'a') as f:
            f.write(f'{pip_package}>={version}\n')

        return True

    def perform(self, command : Actions, *args, **kwargs) -> Any:
        """
        Perform an action given a (string) command

        Args:
            command (Actions): The action to perform.
            *args: Any arguments to pass to the action.
            **kwargs: Any keyword arguments to pass to the action.

        Returns:
            Any: The result of the action.
        """
        # Save the command for later
        self._command = command

        # Determine what method to run.
        match command:
            case Actions.INSTALL:
                # Install a python package using pip, and add it (with version) to requirements.txt.
                package_name = self.get_argument('package_name', args, kwargs)
                return self.pip_install(package_name)
            case _:
                # Run the parent method
                return super().perform(command, *args, **kwargs)

def main():
    """
    This code is only run when this script is called directly (i.e. python bin/app.py)
    """
    try:
        parser = argparse.ArgumentParser(description='Setup and manage the Django application (similar to manage.py).')
        parser.add_argument('action', choices=[e.value for e in Actions], help='The action to perform.')
        parser.add_argument('-p', '--project-name', default='myproject', help='The name of the project.')
        parser.add_argument('-a', '--author-name', help='The name of the author.')
        parser.add_argument('-d', '--directory', default='.', help='The directory for the project.')
        parser.add_argument('-f', '--frontend-dir', default='frontend', help='The directory for the frontend (relative to -d).')
        parser.add_argument('-b', '--backend-dir', default='backend', help='The directory for the backend (relative to -d).')
        parser.add_argument('-s', '--settings', default='conf/settings.yaml', help='The settings file to use.')
        parser.add_argument('--page-name', help='The name of the page to create.')
        parser.add_argument('--model-name', help='The name of the model to create.')
        parser.add_argument('--package-name', help='The name of the package to create.')

        # Parse the arguments provided to our script from the command line
        # These are used as attributes. For example: options.action
        options = parser.parse_args()

        try:
            # Load settings
            settings = Settings(options.settings)

            # Instantiate a new App object based on our arguments
            app = App()
            app = App(options.project_name, options.author_name, settings, options.directory, options.frontend_dir, options.backend_dir)

        except ValueError as ve:
            # One of the options contains bad data. Print the message and exit.
            logger.error(f'Bad option provided: {ve}')
            exit()

        except FileNotFoundError as fnf:
            # The options were okay, but we can't find a necessary file (probably the executable)
            logger.error(f'Unable to find a necessary file: {fnf}')
            exit()

        try:
            command = Actions(options.action)
            result = app.perform(command, page_name=options.page_name, model_name=options.model_name, package_name=options.package_name)

            if result is not None:
                logger.debug(f'App returned ({result})')
        except UnsupportedCommandError:
            logger.error("Error: Unknown action. Try --help to see how to call this script.")
            exit()

    except KeyboardInterrupt:
        logger.info('Shutting down server...')
        exit()
    except DbStartError:
        logger.error('Could not start DB. Cannot continue')
        exit()

if __name__ == '__main__':
    """
    This code is only run when this script is called directly (i.e. python bin/app.py)
    """
    main()
if __name__ == '__main__':
    main()