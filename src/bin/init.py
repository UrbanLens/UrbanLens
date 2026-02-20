"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    init.py                                                                                              *
*        Path:    /bin/init.py                                                                                         *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

logger = logging.getLogger(__name__)


class UnrecoverableError(Exception):
    """
    An error that cannot be recovered. This will result in a sys.exit(1)
    """


ROOT_DIR = Path("/app")
SRC_DIR = ROOT_DIR / "src"
APP_DIR = SRC_DIR / "urbanlens"


class DjangoProjectInitializer:
    _db_host: str
    _db_port: int
    _db_name: str
    _db_user: str
    _db_pass: str
    _environment: str

    def __init__(self, no_runserver: bool = False, environment: str | None = None):
        self.no_runserver = no_runserver

        # Get database details from environment variables
        self.db_host = os.environ.get("UL_DB_HOST", "localhost")
        self.db_port = int(os.environ.get("UL_DB_PORT", 5432))
        self.db_name = os.environ.get("UL_DB_NAME", "UrbanLens")
        self.db_user = os.environ.get("UL_DB_USER", "postgres")
        self.db_pass = os.environ.get("UL_DB_PASS", "postgres")
        self.environment = environment or os.environ.get("ENVIRONMENT", "production")

    @property
    def db_host(self) -> str:
        return self._db_host

    @db_host.setter
    def db_host(self, value: str):
        # Strip special characters from the host
        self._db_host = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_host:
            # Only log the "safe" value to prevent injection attacks into the logfile
            logger.error("Invalid host name. Stripped special characters to %s", self._db_host)
            raise UnrecoverableError("Invalid host name.")

    @property
    def db_port(self) -> int:
        return self._db_port

    @db_port.setter
    def db_port(self, value: int):
        # validate that value is a number
        try:
            self._db_port = int(value)
        except ValueError:
            logger.error("Invalid port number")
            raise UnrecoverableError

    @property
    def db_name(self) -> str:
        return self._db_name

    @db_name.setter
    def db_name(self, value: str):
        # Strip special characters from the name
        self._db_name = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_name:
            # Only log the "safe" value to prevent injection attacks into the logfile
            logger.error("Invalid database name. Stripped special characters to %s", self._db_name)
            raise UnrecoverableError("Invalid database name.")

    @property
    def db_user(self) -> str:
        return self._db_user

    @db_user.setter
    def db_user(self, value: str):
        # Strip special characters from the user
        self._db_user = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_user:
            # Only log the "safe" value to prevent injection attacks into the logfile
            logger.error("Invalid database user. Stripped special characters to %s", self._db_user)
            raise UnrecoverableError("Invalid database user.")

    @property
    def db_pass(self) -> str:
        return self._db_pass

    @db_pass.setter
    def db_pass(self, value: str):
        # Strip special characters from the pass
        self._db_pass = re.sub(r"[^a-zA-Z0-9!@#$^*()_-]", "", value)
        if value != self._db_pass:
            # Only log the "safe" value to prevent injection attacks into the logfile
            logger.error("Invalid database password. Stripped special characters.")
            raise UnrecoverableError("Invalid database password.")

    @property
    def environment(self) -> str:
        return self._environment

    @environment.setter
    def environment(self, value: str):
        # Ensure environment is a known option
        if value not in ["development", "test", "production"]:
            safe_value = re.sub(r"[^a-zA-Z0-9_-]", "", value)
            logger.error(f"Invalid environment: {safe_value}")
            raise UnrecoverableError(f"Invalid environment: {safe_value}")

        self._environment = value

    def configure_git(self):
        """
        Configures git with the provided username and email

        Raises:
            UnrecoverableError: if the git configuration fails

        """
        git_user = os.environ.get("GIT_NAME")
        git_email = os.environ.get("GIT_EMAIL")
        try:
            if git_user:
                subprocess.run(["git", "config", "--global", "user.name", git_user], check=True, cwd="/app")
            if git_email:
                subprocess.run(["git", "config", "--global", "user.email", git_email], check=True, cwd="/app")
            logger.info("Git configured with username %s and email %s.", git_user, git_email)
            
        except subprocess.CalledProcessError as e:
            logger.error("Error configuring git: %s", e)

    def init_db(self):
        """
        Create the "UrbanLens" database in postgres
        """
        self.create_pgpass()
        
        if self.check_db():
            logger.info("Database %s already exists.", self.db_name)
            return

        logger.info("Database %s does not exist. Creating...", self.db_name)

        # Create the database
        self.run_command(["psql", "-U", self.db_user, "-h", self.db_host, "-w", "-c", f"CREATE DATABASE {self.db_name}"], "creating database")

        if not self.check_db():
            logger.error("Database %s was not created.", self.db_name)
            raise UnrecoverableError(f"Database {self.db_name} was not created.")
        
    def copy_sample_env(self):
        """
        Copies .env-sample to .env

        Raises:
            UnrecoverableError: if the file cannot be copied

        """
        if Path("/app/.env").exists():
            return
        
        try:
            with open("/app/.env-sample") as sample_file:
                sample_data = sample_file.read()
            with open("/app/.env", "w") as new_file:
                new_file.write(sample_data)
            logger.info("Copied .env-sample to .env.")
        except OSError as e:
            logger.error(f"Error copying .env-sample: {e}")
            raise UnrecoverableError from e

        # Check that it now exists
        if not Path("/app/.env").exists():
            logger.error(".env was copied but still does not exist.")
            raise UnrecoverableError(".env was copied but still does not exist.")

    def update_env(self, username: str, email: str):
        """
        Updates the env file with the git username and email

        Probably deprecated

        Args:
            username (str): git username
            email (str): git email

        Raises:
            UnrecoverableError: if the file cannot be updated

        """
        try:
            with open("/app/.env") as file:
                data = file.readlines()

            for i, line in enumerate(data):
                if line.startswith("GIT_USERNAME="):
                    data[i] = f"GIT_USERNAME={username}\n"
                elif line.startswith("GIT_EMAIL="):
                    data[i] = f"GIT_EMAIL={email}\n"

            with open("/app/.env", "w") as file:
                file.writelines(data)
            logger.info("Updated git username and email in .env.")
        except OSError as e:
            logger.error(f"Error updating .env: {e}")
            raise UnrecoverableError from e

        # Check that it now exists
        if not Path("/app/.env").exists():
            logger.error(".env was updated but still does not exist.")
            raise UnrecoverableError(".env was updated but still does not exist.")

    def npm_init(self):
        """
        Runs npm init.

        This should ideally be performed within Docker so that the results can be cached. 
        npm typically takes a long time to install.

        Raises:
            UnrecoverableError: if npm init fails

        """
        self.run_command(["npm", "install", "-y"], "during npm init")

    def build_frontend(self):
        """
        Builds the frontend

        Raises:
            UnrecoverableError: if the frontend fails to build

        """
        # First, ensure that all directories for build files exist.
        # This is necessary because the build process will not create them, and will fail if they do not exist.
        apps = ["dashboard", "core"]
        dirs: list[Path] = []
        for app in apps:
            dirs.append(APP_DIR / app / "frontend" / "static" / app / "js")
            dirs.append(APP_DIR / app / "frontend" / "static" / app / "css")

        for dir in dirs:
            if not dir.exists():
                os.makedirs(dir)
                logger.debug(f"Created directory {dir}")

        # Ensure entrypoint (dashboard/frontend/static/dashboard/js/index.js) exists
        entry = APP_DIR / "dashboard" / "frontend" / "static" / "dashboard" / "js" / "index.js"
        if not entry.exists():
            with open(entry, "w") as file:
                file.write("")
            logger.debug(f"Created empty file {entry}")

        self.run_command(["npm", "run", "sass"], "compiling sass", raise_error=False)

        match self.environment:
            case "development":
                command = ["npm", "run", "build"]
            case _:
                command = ["npm", "run", "deploy"]

        self.run_command(command, "building frontend")
        self.run_command(["python", "src/urbanlens/manage.py", "collectstatic", "--noinput"], "collecting static files")
        
    def run_migrations(self):
        """
        Runs django migrations (i.e. creates the django DB tables)

        Raises:
            UnrecoverableError: if the migrations fails

        """
        self.run_command(["python", "src/urbanlens/manage.py", "migrate"], "migrating db")

    def run_command(self, command: list[str], description: str | None = None, cwd: str | Path = "/app", raise_error: bool = True) -> bool:
        """
        Run a command

        Args:
            command (List[str]): the command to run
            description (str): a description of the command
            cwd (str): the directory to run the command in

        Raises:
            UnrecoverableError: if the command fails

        """
        try:
            subprocess.run(command, check=True, cwd=cwd)
            return True
        
        except subprocess.CalledProcessError as e:
            description = description or "running command: " + " ".join(command)
            logger.error("Error occurred %s: %s", description, e)
            
            if raise_error:
                raise UnrecoverableError from e
            
            return False

    def run_dev_server(self):
        """
        Runs the development server

        Raises:
            UnrecoverableError: if the server fails to run

        """
        self.run_command(["python", "src/urbanlens/manage.py", "runserver"], "running development server")

    def run_prod_server(self):
        """
        Runs the production server

        Raises:
            UnrecoverableError: if the server fails to run

        """
        self.run_command(["npm", "run", "start"], "running production server")

    def check_network(self) -> bool:
        """
        Checks that we have a functioning network connection

        Returns:
            bool: True if the network is functioning, False otherwise

        """
        # Check that we can ping google.com
        return self.run_command(["ping", "-c", "1", "google.com"], "checking network connection", raise_error=False)

    def create_pgpass(self):
        """
        Creates a .pgpass file for the database connection

        Raises:
            UnrecoverableError: if the file cannot be created

        """
        pgpass = os.path.expanduser("~/.pgpass")
        if Path(pgpass).exists():
            logger.debug(".pgpass file already exists.")
            return
        
        try:
            with open(pgpass, "w") as file:
                file.write(f"{self.db_host}:{self.db_port}:*:{self.db_user}:{self.db_pass}\n")
            os.chmod(pgpass, 0o600)
            # file_contents = open(pgpass, 'r').read()
            # logger.debug('Created .pgpass file: %s', file_contents)
        except OSError as e:
            logger.error(f"Error creating .pgpass file: {e}")
            raise UnrecoverableError from e

    def check_dependencies(self):
        """
        Todo:

        """
        raise NotImplementedError

    def install_dependencies(self):
        """
        Todo:

        """
        raise NotImplementedError

    def check_db(self) -> bool:
        """
        Checks that the database exists.

        Returns:
            bool: True if the database exists, False otherwise

        """
        command = ["psql", "-U", self.db_user, "-h", self.db_host, "-p", str(self.db_port), "-w", "-c", f"SELECT 1 FROM pg_database WHERE datname='{self.db_name}'"]
        return self.run_command(command, "checking database", raise_error=False)

    def initialize_project(self):
        """
        Initializes the project for the first time

        Raises:
            UnrecoverableError: if the project cannot be initialized

        """
        # Clone the repo
        if not Path("/app").exists():
            logger.warning("Project source files cannot be found.")
            return

        """
        if not self.check_ssh_keys():
            logger.error('SSH keys are not valid. Cannot initialize project.')
            raise UnrecoverableError("SSH keys are not valid. Cannot initialize project.")
        self.clone_repo()
        """
        self.copy_sample_env()

        # Install and build the frontend
        # self.npm_init()
        self.build_frontend()

        # Setup the DB
        self.init_db()
        self.run_migrations()

        if not self.no_runserver:
            self.run_prod_server()


def main():
    """
    Run the initializer.
    """
    logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(os.path.join("/var", "log", "urbanlens", "init.log")),
            ],
        )

    parser = argparse.ArgumentParser(description="Initialize Django project and run server")
    parser.add_argument("--no-runserver", "-x", action="store_true", help="Do not run the development server after migration")
    parser.add_argument("--debug", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--environment", "-e", choices=["development", "test", "production"], help="Set the environment")
    args = parser.parse_args()

    if args.debug:
        # Replace root logger after config change (I'm not certain this is necessary TODO)
        # logger = logging.getLogger(__name__)
        # Change the loglevel
        logger.setLevel(logging.DEBUG)
        logger.info("Debug logging enabled.")

    try:
        initializer = DjangoProjectInitializer(no_runserver=args.no_runserver, environment=args.environment)
        initializer.initialize_project()
    except KeyboardInterrupt:
        logger.info("Initialization cancelled.")
        sys.exit(0)
    except UnrecoverableError:
        logger.error("Initialization failed.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    """
    If the script is called directly...
    """
    main()
