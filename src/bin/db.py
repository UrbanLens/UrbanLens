# !/usr/bin/env python

import argparse
from enum import Enum
import logging
import os
from pathlib import Path
import re
from shutil import which
import subprocess  # nosec B404
import sys
import textwrap
import time

from dotenv import load_dotenv
from tqdm import tqdm

from bin.utils.action import EnumAction

logger = logging.getLogger(__name__)

# The project root: src/bin/db.py → src/ → project root
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# Data/log defaults are relative to the src/ directory (original behaviour).
DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Default path to the data directory, which we pass directly to postgres
DEFAULT_DATA_PATH = os.environ.get("URBANLENS_DB_DATA_PATH", f"{DIR}/pgsql/data")
# Default path to the logfile we want to use.
DEFAULT_LOG_PATH = os.environ.get("URBANLENS_LOG_PATH", f"{DIR}/pgsql/pgsql.log")
# Command to use to interact with the DB. This must be in our path.
EXE = os.environ.get("URBANLENS_POSTGRES_BIN", "pg_ctl")


def _resolve_executable(name: str) -> str:
    """Return an absolute executable path, failing closed when it is unavailable."""
    resolved = which(name) if not os.path.isabs(name) else name
    if resolved is None:
        raise FileNotFoundError(f'Required executable not found on PATH: "{name}"')
    return str(Path(resolved).resolve())


PSQL_EXE = os.environ.get("URBANLENS_PSQL_BIN", "psql")
PG_DUMP_EXE = os.environ.get("URBANLENS_PG_DUMP_BIN", "pg_dump")
PYTHON_EXE = str(Path(sys.executable).resolve())


class Db:
    _data_path: str
    _log_path: str
    _user: str
    _database: str
    _host: str
    _port: int

    @property
    def log_path(self) -> str:
        return self._log_path

    @property
    def data_path(self) -> str:
        return self._data_path

    @property
    def user(self) -> str:
        return self._user

    @property
    def database(self) -> str:
        return self._database

    @property
    def socket_dir(self) -> Path:
        """Local directory for postgres socket/lock files (avoids /var/run/postgresql permission issues)."""
        return Path(self._data_path).parent / "tmp" / "socket"

    @log_path.setter
    def log_path(self, user_input_path: str) -> None:
        """
        Sets the log path. Assumes that input_path is user input and sanitizes it accordingly.

        Args:
            user_input_path (str): The path provided via user input to sanitize and set.

        Returns:
            None

        """
        self._log_path = self.sanitize_path(user_input_path)

    @data_path.setter
    def data_path(self, user_input_path: str) -> None:
        """
        Sets the data directory path. Assumes that input_path is user input and sanitizes it accordingly.

        Args:
            user_input_path(str): The path provided via user input to sanitize and set.

        Returns:
            None

        """
        self._data_path = self.sanitize_path(user_input_path)

    def __init__(self, data_path: str = DEFAULT_DATA_PATH, log_path: str = DEFAULT_LOG_PATH):
        """
        Sets up our Db object with config options we'll use for this run.

        Args:
            data_path (str, optional):
                The data directory path to use, which is passed directly to Postgres.
                Note: This is sanitized and only accepts these characters: a-zA-Z0-9/_.-
                On windows, this also accepts colons and backslashes.
                Defaults to the DEFAULT_DATA_PATH constant.
            log_path:
                The logfile we want Postgres to use.
                Note: This is sanitized and only accepts these characters: a-zA-Z0-9/_.-
                On windows, this also accepts colons and backslashes.
                Defaults to the DEFAULT_LOG_PATH constant.

        Raises:
            ValueError: If the config options provided are not valid, or the files they reference are not found.
            FileNotFoundError: If the postgres executable cannot be found.

        """
        load_dotenv(ROOT_DIR / ".env")

        # Validation
        if not os.path.isdir(data_path):
            raise ValueError(f'Data path not found: "{data_path}"')
        if not os.path.isfile(log_path):
            raise ValueError(f'Log path not found: "{log_path}"')
        if which(EXE) is None and not os.path.exists(EXE):
            raise FileNotFoundError(f'DB executable not found. Is "{EXE}" in your path?')

        # Set our paths. Note: This calls the property setter, which sanitizes them.
        self.data_path = data_path
        self.log_path = log_path

        self._user = os.environ.get("UL_DB_USER", "urbanlens")
        self._database = os.environ.get("UL_DB_NAME", "urbanlens")
        self._host = os.environ.get("UL_DB_HOST", "localhost")
        self._port = int(os.environ.get("UL_DB_PORT", "5432"))

    def _pg_ctl(
        self,
        command: str,
        with_server_opts: bool = False,
        pg_wait: bool = False,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        """Build and run a pg_ctl command.

        Args:
            command (str): The pg_ctl subcommand (start, stop, status, restart, ...).
            with_server_opts (bool): When True, pass socket dir and port via -o so the
                server process uses them.  Should be True for start/restart only.
            pg_wait (bool): When True, pass -w to pg_ctl so it blocks until the server
                is ready to accept connections.
            **kwargs: Forwarded to subprocess.run.

        Returns:
            subprocess.CompletedProcess: The completed process result.

        """
        cmd = [_resolve_executable(EXE), "-D", self.data_path, "-l", self.log_path]
        if pg_wait:
            cmd.append("-w")
        if with_server_opts:
            self.socket_dir.mkdir(parents=True, exist_ok=True)
            server_opts = [f"-k {self.socket_dir}", f"-p {self._port}"]
            cmd += ["-o", " ".join(server_opts)]
        cmd.append(command)
        kwargs.setdefault("check", True)
        return subprocess.run(cmd, **kwargs)  # noqa: PLW1510  # nosec B603

    def execute_sql(self, sql: str, database: str | None = None) -> int:
        """Run a SQL statement via psql and return the exit code.

        Args:
            sql (str): The SQL statement to execute.
            database (str | None): Database to connect to; defaults to self.database.

        Returns:
            int: psql exit code (0 on success).

        """
        cmd = [
            _resolve_executable(PSQL_EXE),
            "-U",
            self.user,
            "-h",
            self._host,
            "-p",
            str(self._port),
            "-d",
            database or self.database,
            "-c",
            sql,
        ]
        return subprocess.call(cmd)  # nosec B603

    def start(self) -> None:
        """Start the postgres server if it is not already running."""
        if self.is_running():
            print("Postgres server already running")
            return
        self._pg_ctl("start", with_server_opts=True)

    def restart(self) -> None:
        """Restart the postgres server."""
        self._pg_ctl("restart", with_server_opts=True)

    def stop(self) -> None:
        """Stop the postgres server."""
        self._pg_ctl("stop")

    def status(self) -> None:
        """Print the postgres server status to stdout."""
        self._pg_ctl("status", check=False)

    def check_errors(self) -> int:
        """Report database conflicts for the current database."""
        return self.execute_sql("SELECT * FROM pg_stat_database_conflicts WHERE datname = current_database();")

    def analyze(self) -> int:
        """Run ANALYZE VERBOSE on the database."""
        return self.execute_sql("ANALYZE VERBOSE;")

    def repair_errors(self) -> int:
        """Reindex the database to repair errors."""
        return self.execute_sql("REINDEX DATABASE current_database;")

    def dead_rows(self) -> int:
        """Report tables with dead rows."""
        return self.execute_sql("SELECT relname, n_dead_tup FROM pg_stat_user_tables WHERE n_dead_tup > 0;")

    def long_queries(self) -> int:
        """Report queries running longer than 5 minutes."""
        return self.execute_sql(
            "SELECT pid, now() - pg_stat_activity.query_start AS duration, query FROM pg_stat_activity WHERE (now() - pg_stat_activity.query_start) > interval '5 minutes';",
        )

    def locks(self) -> int:
        """Report ungranted locks."""
        return self.execute_sql("SELECT pid, relation::regclass, mode, granted FROM pg_locks WHERE NOT granted;")

    def backup(self) -> int:
        """Back up the database to a timestamped SQL file.

        Returns:
            int: The PID of the pg_dump process, or -1 on failure.

        """
        BACKUP_DIR = "Z:/DEV/backups/db/postgres"
        name = "backup.sql"

        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
            if not os.path.exists(BACKUP_DIR):
                raise ValueError(f'Backup directory not found and could not be created: "{BACKUP_DIR}"')

        if os.path.exists(os.path.join(BACKUP_DIR, name)):
            count = 0
            while os.path.exists(os.path.join(BACKUP_DIR, name)):
                count += 1
                name = f"backup_{int(time.time())!s}_{count}.sql"

        try:
            cmd = [_resolve_executable(PG_DUMP_EXE), "-U", self.user, "-d", self.database, "-f", os.path.join(BACKUP_DIR, name)]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec B603

            with tqdm(total=100) as progress_bar:
                while process.poll() is None:
                    progress_bar.update(1)
                    time.sleep(1)

            _stdout, stderr = process.communicate()
            if process.returncode != 0:
                logger.error("pg_dump failed with error: %s", stderr.decode("utf-8"))

            return process.pid

        except (OSError, subprocess.SubprocessError, ValueError) as e:
            logger.exception("Error backing up database: %s", e)

        return -1

    def manage(self) -> None:
        """Continuously ensure the postgres server is running."""
        while True:
            if not self.is_running():
                print("Postgres is not running. Starting it up...")
                self.start()
            else:
                print("Postgres is running.")
            time.sleep(5)

    def is_running(self) -> bool:
        """
        Determines if the postgres server is running, without printing anything to stdout.

        Returns:
            bool: True if the server is running, False otherwise.

        Raises:
            FileNotFoundError: If postgres is not able to find the data directory

        """
        child = self._pg_ctl("status", check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if child.returncode == 4:
            raise FileNotFoundError(f"Postgres is not able to find the data directory: {self.data_path}")
        return child.returncode == 0

    def sanitize_path(self, user_input_path: str) -> str:
        """
        Takes arbitrary user input, and sanitizes it to prevent injection attacks.

        NOTE: The return value from this function will generally be passed directly to the command line,
        so we must be especially careful with what we return.

        Args:
            user_input_path (str): The user input to turn into a path

        Returns:
            str: The sanitized path

        """
        if os.name == "nt":
            return re.sub(r"[^a-zA-Z0-9:/\\_.-]", "", user_input_path)
        return re.sub(r"[^a-zA-Z0-9/_.-]", "", user_input_path)

    def run_action(self, action: "Actions") -> None:
        """Dispatch to the method matching the action name.

        Args:
            action (Actions): The action to run.

        Raises:
            SystemExit: If the action is unknown.

        """
        method = getattr(self, action.value, None)
        if method is None or not callable(method):
            print(f"Error: Unknown action '{action}'. Try --help to see how to call this script.")
            sys.exit(1)
        method()


class DbInitializer:
    """Handles first-time database setup: create role, create DB, enable PostGIS, run migrations."""

    def __init__(self):
        """Read connection parameters from environment variables.

        Defaults match Django settings/base.py so behaviour is consistent
        whether the caller sets the env vars or not.
        """
        load_dotenv(ROOT_DIR / ".env")
        self.db_host = os.environ.get("UL_DB_HOST", "localhost")
        self.db_port = os.environ.get("UL_DB_PORT", "5432")
        self.db_name = os.environ.get("UL_DB_NAME", "urbanlens")
        self.db_user = os.environ.get("UL_DB_USER", "urbanlens")
        self.db_pass = os.environ.get("UL_DB_PASS", "")

    def run(self) -> None:
        """Run the full database initialisation sequence.

        Steps: create .pgpass → ensure role exists → create DB + enable PostGIS → run migrations.
        """
        self.create_pgpass()
        self._ensure_role()
        self.init_db()
        self.run_migrations()

    def create_pgpass(self) -> None:
        """Write a ~/.pgpass entry so psql commands don't prompt for a password."""
        pgpass = Path(os.path.expanduser("~/.pgpass"))
        if pgpass.exists():
            logger.debug(".pgpass file already exists.")
            return

        try:
            pgpass.write_text(f"{self.db_host}:{self.db_port}:*:{self.db_user}:{self.db_pass}\n", encoding="utf-8")
            pgpass.chmod(0o600)
        except OSError as e:
            logger.exception("Error creating .pgpass file: %s", e)
            raise

    def _psql_env(self) -> dict[str, str]:
        """Return an env dict with PGPASSWORD set for psql subprocesses."""
        env = os.environ.copy()
        env["PGPASSWORD"] = self.db_pass
        return env

    def execute_sql(
        self,
        sql: str,
        database: str | None = None,
        check: bool = True,
        variables: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a SQL statement via psql against the maintenance database.

        Args:
            sql (str): The SQL statement to execute.
            database (str | None): Database to connect to; defaults to 'postgres' maintenance DB.
            check (bool): If True, raise CalledProcessError on non-zero exit.

        Returns:
            subprocess.CompletedProcess: The completed process result.

        """
        cmd = [
            _resolve_executable(PSQL_EXE),
            "-U",
            self.db_user,
            "-h",
            self.db_host,
            "-p",
            self.db_port,
            "-d",
            database or "postgres",
            "-w",
        ]
        for key, value in (variables or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid psql variable name: {key!r}")
            cmd.extend(["-v", f"{key}={value}"])
        if variables:
            # psql expands :'var' only when SQL is read from stdin, not via -c.
            return subprocess.run(  # nosec B603
                cmd,
                env=self._psql_env(),
                input=sql.encode(),
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        cmd.extend(["-c", sql])
        return subprocess.run(cmd, env=self._psql_env(), check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec B603

    def _check_role_exists(self) -> bool:
        """Return True if the db_user role already exists in this cluster.

        Uses COUNT(*) and checks stdout because a SELECT that finds no rows
        still exits with code 0 - only the output distinguishes the cases.

        Returns:
            bool: True if the role exists, False otherwise.

        """
        result = self.execute_sql(
            "SELECT COUNT(*) FROM pg_roles WHERE rolname = :'role_name'",
            check=False,
            variables={"role_name": self.db_user},
        )
        return result.returncode == 0 and b"1" in result.stdout

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        """Safely quote a PostgreSQL identifier for trusted administrative DDL."""
        if "\x00" in identifier:
            raise ValueError("PostgreSQL identifiers cannot contain NUL bytes")
        return '"' + identifier.replace('"', '""') + '"'

    def _ensure_role(self) -> None:
        """Create the application login role if it does not already exist."""
        if self._check_role_exists():
            logger.info("Role %s already exists.", self.db_user)
            return
        logger.info("Creating role %s ...", self.db_user)
        self.execute_sql(f"CREATE ROLE {self._quote_identifier(self.db_user)} WITH LOGIN SUPERUSER")

    def check_db(self) -> bool:
        """Return True if the application database already exists.

        Attempts a direct connection to the target database rather than
        querying pg_database - a SELECT always exits 0 even with no rows,
        but a connection to a non-existent database exits non-zero.

        Returns:
            bool: True if the database exists and is reachable, False otherwise.

        """
        result = subprocess.run(  # nosec B603
            [
                _resolve_executable(PSQL_EXE),
                "-U",
                self.db_user,
                "-h",
                self.db_host,
                "-p",
                self.db_port,
                "-d",
                self.db_name,
                "-w",
                "-c",
                "SELECT 1",
            ],
            env=self._psql_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode == 0

    def enable_postgis(self) -> None:
        """Enable the PostGIS extension in the application database (idempotent)."""
        self.execute_sql("CREATE EXTENSION IF NOT EXISTS postgis", database=self.db_name, check=False)

    def init_db(self) -> None:
        """Create the application database and enable PostGIS if not already present."""
        if self.check_db():
            logger.info("Database %s already exists.", self.db_name)
        else:
            logger.info("Database %s does not exist. Creating...", self.db_name)
            self.execute_sql(f"CREATE DATABASE {self._quote_identifier(self.db_name)}")
            if not self.check_db():
                raise RuntimeError(f"Database {self.db_name} was not created.")
        self.enable_postgis()

    def run_migrations(self) -> None:
        """Run Django database migrations.

        Passes UL_DB_PASS explicitly so Django's psycopg2 connection has the
        password even when it isn't already in the shell environment.

        Raises:
            subprocess.CalledProcessError: if the migration command fails.

        """
        manage = ROOT_DIR / "src" / "urbanlens" / "manage.py"
        env = os.environ.copy()
        env.setdefault("UL_DB_PASS", self.db_pass)
        subprocess.run([PYTHON_EXE, str(manage), "migrate"], check=True, cwd=ROOT_DIR, env=env)  # nosec B603


class Actions(Enum):
    """
    Defines the options we allow to be passed in from the command line when this script is run.

    Attributes:
        status: check the DB status
        start: start the DB (if it is not already running)
        restart: stop the DB (if it is running) and start it again.
        stop: stop the DB (if it is running)
        init: initialize the project (create DB, run migrations)
    """

    start = "start"
    restart = "restart"
    status = "status"
    stop = "stop"
    check_errors = "check_errors"
    analyze = "analyze"
    repair_errors = "repair_errors"
    manage = "manage"
    dead_rows = "dead_rows"
    long_queries = "long_queries"
    locks = "locks"
    init = "init"

    def __str__(self):
        """Turns an option into a string representation."""
        return self.value


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description=textwrap.dedent("""
                Interact with the application's local DB
            """),
        epilog="",
    )

    parser.add_argument(
        "action",
        type=Actions,
        action=EnumAction,
        help=textwrap.dedent("""\
                        Interact with the application DB or project initializer

                        status: check the DB status
                        start: start the DB (if it is not already running)
                        restart: stop the DB (if it is running) and start it again.
                        stop: stop the DB (if it is running)
                        init: initialize the project (create DB, run migrations)
                     """),
    )
    parser.add_argument(
        "-l",
        "--log",
        type=str,
        metavar="path",
        default=DEFAULT_LOG_PATH,
        help="Path to the log file for the DB.",
    )
    parser.add_argument(
        "-d",
        "--data",
        type=str,
        metavar="path",
        default=DEFAULT_DATA_PATH,
        help="Path to the data directory for postgres.",
    )

    options = parser.parse_args()

    if options.action == Actions.init:
        # Load .env early so UL_DB_PORT/UL_DB_USER are available for cluster config.
        load_dotenv(ROOT_DIR / ".env")
        db_port = os.environ.get("UL_DB_PORT", "5432")
        db_user = os.environ.get("UL_DB_USER", "urbanlens")
        print(f"Using postgres port: {db_port}, superuser: {db_user}")

        data_path = Path(options.data)
        log_path = Path(options.log)

        # Create directories and an empty log file if they don't exist yet.
        data_path.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.touch()

        # Initialize the postgres cluster if the data directory is empty.
        if not (data_path / "PG_VERSION").exists():
            logger.info("Initializing postgres cluster at %s ...", data_path)
            try:
                # --username sets the superuser name so it matches UL_DB_USER.
                # Without this, initdb defaults to the OS user.
                subprocess.run(  # nosec B603
                    [_resolve_executable(EXE), "initdb", "-D", str(data_path), "-o", f"--username={db_user}"],
                    check=True,
                )
            except subprocess.CalledProcessError:
                logger.exception("pg_ctl initdb failed.")
                sys.exit(1)

        # Patch postgresql.conf for local dev.  Runs on every init so the config
        # is always consistent even after a failed previous start.
        conf_path = data_path / "postgresql.conf"
        if conf_path.exists():
            conf = conf_path.read_text(encoding="utf-8")
            conf = re.sub(r"^#?port\s*=\s*\d+", f"port = {db_port}", conf, flags=re.MULTILINE)
            # Use the data directory for socket/lock files - /var/run/postgresql requires root.
            conf = re.sub(
                r"^#?unix_socket_directories\s*=\s*'[^']*'",
                f"unix_socket_directories = '{data_path}'",
                conf,
                flags=re.MULTILINE,
            )
            conf_path.write_text(conf, encoding="utf-8")
            print(f"Configured postgresql.conf (port={db_port}, socket dir={data_path})")

        # Now that paths exist we can build the Db wrapper.
        try:
            db = Db(data_path=str(data_path), log_path=str(log_path))
        except (ValueError, FileNotFoundError) as e:
            print(f"Cannot initialize postgres: {e}")
            sys.exit(1)

        # Start the server and wait until it is ready to accept connections.
        if not db.is_running():
            print(f"Starting postgres server on port {db_port} ...")
            try:
                db._pg_ctl("start", with_server_opts=True, pg_wait=True)  # noqa: SLF001
            except subprocess.CalledProcessError:
                print(f"Failed to start postgres. Log ({log_path}):")
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    print("\n".join(lines[-30:]))
                except OSError as log_err:
                    print(f"  (could not read log: {log_err})")
                sys.exit(1)

        # Create the application database, enable PostGIS, and run migrations.
        try:
            DbInitializer().run()
        except (subprocess.CalledProcessError, RuntimeError, OSError):
            logger.exception("Database initialization failed.")
            sys.exit(1)
        sys.exit(0)

    try:
        db = Db(data_path=options.data, log_path=options.log)
    except ValueError as ve:
        print(f"Bad option provided: {ve}")
        sys.exit()
    except FileNotFoundError as fnf:
        print(f"Unable to find a necessary file: {fnf}")
        sys.exit()

    db.run_action(options.action)
    sys.exit()


if __name__ == "__main__":
    main()
