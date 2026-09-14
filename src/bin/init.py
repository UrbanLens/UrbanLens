from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
from shutil import which
import subprocess  # nosec B404
import sys

logger = logging.getLogger(__name__)


class UnrecoverableError(Exception):
    """Fatal error; results in sys.exit(1)."""


def resolve_executable(name: str) -> str:
    """Return an absolute path for an executable on PATH."""
    resolved = which(name)
    if resolved is None:
        raise UnrecoverableError(f"Required executable not found on PATH: {name}")
    return resolved


# Resolve from this file's location: src/bin/init.py → src/ → project root
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
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

        self.db_host = os.environ.get("UL_DB_HOST", "localhost")
        self.db_port = int(os.environ.get("UL_DB_PORT", "5432"))
        self.db_name = os.environ.get("UL_DB_NAME", "UrbanLens")
        self.db_user = os.environ.get("UL_DB_USER", "postgres")
        self.db_pass = os.environ.get("UL_DB_PASS", "postgres")
        self.environment = environment or os.environ.get("UL_ENVIRONMENT", "production")
        # Hot-reload bind-mounts make build outputs host-owned; see docker-compose.hot-reload.yml.
        self.skip_frontend_build = os.environ.get("UL_SKIP_FRONTEND_BUILD", "").lower() in {"1", "true", "yes"}

    @property
    def db_host(self) -> str:
        return self._db_host

    @db_host.setter
    def db_host(self, value: str):
        self._db_host = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_host:
            # Log only the safe value to avoid log injection.
            logger.error("Invalid host name. Stripped special characters to %s", self._db_host)
            raise UnrecoverableError("Invalid host name.")

    @property
    def db_port(self) -> int:
        return self._db_port

    @db_port.setter
    def db_port(self, value: int):
        try:
            self._db_port = int(value)
        except ValueError as ve:
            logger.exception("Invalid port number")
            raise UnrecoverableError("Invalid port number") from ve

    @property
    def db_name(self) -> str:
        return self._db_name

    @db_name.setter
    def db_name(self, value: str):
        self._db_name = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_name:
            # Log only the safe value to avoid log injection.
            logger.error("Invalid database name. Stripped special characters to %s", self._db_name)
            raise UnrecoverableError("Invalid database name.")

    @property
    def db_user(self) -> str:
        return self._db_user

    @db_user.setter
    def db_user(self, value: str):
        self._db_user = re.sub(r"[^a-zA-Z0-9_-]", "", value)
        if value != self._db_user:
            # Log only the safe value to avoid log injection.
            logger.error("Invalid database user. Stripped special characters to %s", self._db_user)
            raise UnrecoverableError("Invalid database user.")

    @property
    def db_pass(self) -> str:
        return self._db_pass

    @db_pass.setter
    def db_pass(self, value: str):
        self._db_pass = re.sub(r"[^a-zA-Z0-9!@#$^*()_-]", "", value)
        if value != self._db_pass:
            # Log only the safe value to avoid log injection.
            logger.error("Invalid database password. Stripped special characters.")
            raise UnrecoverableError("Invalid database password.")

    @property
    def environment(self) -> str:
        return self._environment

    @environment.setter
    def environment(self, value: str):
        if value not in {"local", "development", "testing", "production", "staging"}:
            safe_value = re.sub(r"[^a-zA-Z0-9_-]", "", value)
            logger.error("Invalid environment: %s", safe_value)
            raise UnrecoverableError(f"Invalid environment: {safe_value}")

        self._environment = value

    def configure_git(self):
        """Configure git user/email.

        Raises:
            UnrecoverableError: if git configuration fails.
        """
        git_user = os.environ.get("GIT_NAME")
        git_email = os.environ.get("GIT_EMAIL")
        try:
            git_executable = resolve_executable("git")
            if git_user:
                subprocess.run(
                    [git_executable, "config", "--global", "user.name", git_user],
                    check=True,
                    cwd=ROOT_DIR,
                )  # nosec B603
            if git_email:
                subprocess.run(
                    [git_executable, "config", "--global", "user.email", git_email],
                    check=True,
                    cwd=ROOT_DIR,
                )  # nosec B603
            logger.info("Git configured with username %s and email %s.", git_user, git_email)

        except subprocess.CalledProcessError as e:
            logger.exception("Error configuring git: %s", e)

    def enable_postgis(self) -> None:
        """Enable PostGIS (idempotent; call after the DB exists)."""
        self.run_command(
            [
                "psql",
                "-U",
                self.db_user,
                "-h",
                self.db_host,
                "-p",
                str(self.db_port),
                "-w",
                "-d",
                self.db_name,
                "-c",
                "CREATE EXTENSION IF NOT EXISTS postgis",
            ],
            "enabling postgis extension",
            raise_error=False,
        )

    def init_db(self):
        """Create the database and enable PostGIS."""
        self.create_pgpass()

        if self.check_db():
            logger.info("Database %s already exists.", self.db_name)
        else:
            logger.info("Database %s does not exist. Creating...", self.db_name)

            # Create the database
            self.run_command(
                ["psql", "-U", self.db_user, "-h", self.db_host, "-w", "-c", f"CREATE DATABASE {self.db_name}"],
                "creating database",
            )

            if not self.check_db():
                logger.error("Database %s was not created.", self.db_name)
                raise UnrecoverableError(f"Database {self.db_name} was not created.")

        # PostGIS is required for the PointField; idempotent on existing DBs.
        self.enable_postgis()

    #: Where a `.env` is just a checkout convenience; elsewhere env vars rule and `.env` stays out of the image.
    _ENV_FILE_ENVIRONMENTS = frozenset({"local", "development", "testing"})

    def copy_sample_env(self):
        """Seed a local checkout's `.env` from `.env-sample` when needed.

        Skipped in staging/production (configured from the environment) and never fatal.
        """
        env_file = ROOT_DIR / ".env"
        env_sample = ROOT_DIR / ".env-sample"

        if env_file.exists():
            return

        if self.environment not in self._ENV_FILE_ENVIRONMENTS:
            logger.info(
                "No .env in %s, and none is needed: this environment is configured from real environment variables.",
                self.environment,
            )
            return

        try:
            with env_sample.open(encoding="utf-8") as sample_file:
                sample_data = sample_file.read()
            with env_file.open("w", encoding="utf-8") as new_file:
                new_file.write(sample_data)
            logger.info("Copied .env-sample to .env.")
        except OSError:
            logger.warning(
                "Could not create %s from .env-sample; continuing. Set the configuration through the environment instead.",
                env_file,
                exc_info=True,
            )

    def update_env(self, username: str, email: str):
        """Update the env file with git username/email (probably deprecated).

        Args:
            username: git username.
            email: git email.

        Raises:
            UnrecoverableError: if the file cannot be updated.
        """
        env_file = ROOT_DIR / ".env"

        try:
            with env_file.open(encoding="utf-8") as file:
                data = file.readlines()

            for i, line in enumerate(data):
                if line.startswith("GIT_USERNAME="):
                    data[i] = f"GIT_USERNAME={username}\n"
                elif line.startswith("GIT_EMAIL="):
                    data[i] = f"GIT_EMAIL={email}\n"

            with env_file.open("w", encoding="utf-8") as file:
                file.writelines(data)
            logger.info("Updated git username and email in .env.")
        except OSError as e:
            logger.exception("Error updating .env: %s", e)
            raise UnrecoverableError from e

        if not env_file.exists():
            logger.error(".env was updated but still does not exist.")
            raise UnrecoverableError(".env was updated but still does not exist.")

    def bun_init(self):
        """Run bun install.

        Raises:
            UnrecoverableError: if bun install fails.
        """
        self.run_command(["bun", "install", "-y"], "during npm init")

    def build_frontend(self):
        """Build SCSS/TS and collect static (twice: image build and container start).

        Raises:
            UnrecoverableError: if the build fails or the manifest mismatches.
        """
        if self.skip_frontend_build:
            # Bind-mounted checkout: outputs are host-owned and the sidecar rebuilds anyway.
            logger.info("Skipping the frontend build: UL_SKIP_FRONTEND_BUILD is set.")
            return

        # Build dirs must exist or the build fails.
        apps = ["dashboard", "core"]
        dirs: list[Path] = []
        for app in apps:
            dirs.extend(
                (
                    APP_DIR / app / "frontend" / "static" / app / "js",
                    APP_DIR / app / "frontend" / "static" / app / "css",
                ),
            )

        for frontend_dir in dirs:
            if not frontend_dir.exists():
                frontend_dir.mkdir(parents=True, exist_ok=True)
                logger.debug("Created directory %s", frontend_dir)

        match self.environment:
            case "development":
                sass_command = ["bun", "run", "sass:dev"]
                command = ["bun", "run", "build"]
            case _:
                sass_command = ["bun", "run", "sass"]
                command = ["bun", "run", "deploy"]

        self.run_command(sass_command, "compiling sass", raise_error=False)

        self.run_command(command, "building frontend")
        self.run_command(["python", "src/urbanlens/manage.py", "collectstatic", "--noinput"], "collecting static files")
        self.verify_static_manifest()

    def verify_static_manifest(self):
        """Check manifest entries name existing files with portable separators.

        Raises:
            UnrecoverableError: if the manifest is unusable or incomplete.
        """
        manifest = APP_DIR / "frontend" / "static" / "staticfiles.json"
        try:
            entries: dict[str, str] = json.loads(manifest.read_text())["paths"]
        except (OSError, ValueError, KeyError) as exc:
            logger.exception("Static manifest at %s is missing or unreadable.", manifest)
            raise UnrecoverableError(f"Unusable static manifest at {manifest}") from exc

        root = manifest.parent
        # Backslashes name an unresolvable URL even where the file exists.
        separators = sorted(name for name, target in entries.items() if "\\" in target)
        missing = sorted(name for name, target in entries.items() if "\\" not in target and not (root / target).is_file())

        if separators or missing:
            logger.error(
                "Static manifest describes %d entries, %d with a backslash separator (%s) and %d with no file (%s).",
                len(entries),
                len(separators),
                ", ".join(separators[:5]) or "-",
                len(missing),
                ", ".join(missing[:5]) or "-",
            )
            raise UnrecoverableError(f"Static manifest at {manifest} does not match the collected files.")

        # Each build step must have left output; a consistent manifest can still lack all CSS/JS.
        for label, suffix in (("sass", ".css"), ("the bundler", ".js")):
            if not any(name.startswith("dashboard/") and name.endswith(suffix) for name in entries):
                logger.error("Static manifest has %d entries but no dashboard/*%s - %s produced nothing.", len(entries), suffix, label)
                raise UnrecoverableError(f"Static manifest at {manifest} contains no dashboard/*{suffix}; {label} produced nothing.")

        logger.info("Static manifest verified: %d entries, all present.", len(entries))

    def run_migrations(self):
        """Run Django migrations.

        Raises:
            UnrecoverableError: if migrations fail.
        """
        self.run_command(["python", "src/urbanlens/manage.py", "migrate"], "migrating db")

    def run_command(
        self,
        command: list[str],
        description: str | None = None,
        cwd: str | Path | None = None,
        raise_error: bool = True,
    ) -> bool:
        """Run a command.

        Args:
            command: The command to run.
            description: Description of the command.
            cwd: Directory to run in.
            raise_error: Raise on failure.

        Raises:
            UnrecoverableError: if the command fails.
        """
        try:
            resolved_command = [resolve_executable(command[0]), *command[1:]]
            subprocess.run(
                resolved_command,
                check=True,
                cwd=cwd or ROOT_DIR,
            )  # nosec B603
            if any("manage.py" in str(part) for part in command):
                # manage.py prints startup env once per process tree; flag it so later children stay quiet.
                os.environ["UL_STARTUP_ENV_PRINTED"] = "1"
            return True

        except subprocess.CalledProcessError as e:
            description = description or "running command: " + " ".join(command)
            logger.exception("Error occurred %s: %s", description, e)

            if raise_error:
                raise UnrecoverableError from e

            return False

    def run_dev_server(self):
        """Run the development server.

        Raises:
            UnrecoverableError: if the server fails.
        """
        self.run_command(["python", "src/urbanlens/manage.py", "runserver", "0.0.0.0:8000"], "running development server")

    def run_prod_server(self):
        """Run the production server.

        Raises:
            UnrecoverableError: if the server fails.
        """
        self.run_command(["bun", "run", "start"], "running production server")

    def check_network(self) -> bool:
        """Return True when the network is up (pings google.com).

        Returns:
            True if reachable, False otherwise.
        """
        return self.run_command(["ping", "-c", "1", "google.com"], "checking network connection", raise_error=False)

    def create_pgpass(self):
        """Create the .pgpass file.

        Raises:
            UnrecoverableError: if the file cannot be created.
        """
        pgpass = os.path.expanduser("~/.pgpass")
        if Path(pgpass).exists():
            logger.debug(".pgpass file already exists.")
            return

        try:
            Path(pgpass).write_text(
                f"{self.db_host}:{self.db_port}:*:{self.db_user}:{self.db_pass}\n",
                encoding="utf-8",
            )
            os.chmod(pgpass, 0o600)
            # file_contents = open(pgpass, 'r').read()
            # logger.debug('Created .pgpass file: %s', file_contents)
        except OSError as e:
            logger.exception("Error creating .pgpass file: %s", e)
            raise UnrecoverableError from e

    def check_dependencies(self):
        """Not implemented yet."""
        raise NotImplementedError

    def install_dependencies(self):
        """Not implemented yet."""
        raise NotImplementedError

    def check_db(self) -> bool:
        """Return True when the database exists (direct connect).

        Returns:
            True if reachable, False otherwise.
        """
        # psql doesn't expand :'var' in -c, so connect directly instead.
        command = [
            "psql",
            "-U",
            self.db_user,
            "-h",
            self.db_host,
            "-p",
            str(self.db_port),
            "-w",
            "-d",
            self.db_name,
            "-c",
            "SELECT 1",
        ]
        return self.run_command(command, "checking database", raise_error=False)

    def initialize_project(self):
        """Initialize the project.

        Raises:
            UnrecoverableError: if initialization fails.
        """
        # Clone the repo
        if not ROOT_DIR.exists():
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
            if self.environment == "development":
                self.run_dev_server()
            else:
                self.run_prod_server()


def main():
    """Run the initializer."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join("/var", "log", "urbanlens", "init.log")),
        ],
    )

    parser = argparse.ArgumentParser(description="Initialize Django project and run server")
    parser.add_argument(
        "--no-runserver",
        "-x",
        action="store_true",
        help="Do not run the development server after migration",
    )
    parser.add_argument("--debug", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--frontend-only",
        "-f",
        action="store_true",
        help="Build the frontend and collect static files, then exit. Touches no database, so the image build can run it.",
    )
    parser.add_argument(
        "--environment",
        "-e",
        choices=["local", "development", "testing", "production", "staging"],
        help="Set the environment",
    )
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logger.info("Debug logging enabled.")

    try:
        initializer = DjangoProjectInitializer(no_runserver=args.no_runserver, environment=args.environment)
        if args.frontend_only:
            initializer.build_frontend()
        else:
            initializer.initialize_project()
    except KeyboardInterrupt:
        logger.info("Initialization cancelled.")
        sys.exit(0)
    except UnrecoverableError:
        logger.exception("Initialization failed.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
