"""Ephemeral, self-naming dev environments - one per agent, cleaned up on demand.

The previous arrangement was three fixed slots (s1/s2/s3) that ran out, with no
way to tell which were in use. This allocates a fresh name and a free port
block per environment, records what it created, and can list and reclaim them -
so "which environments are busy?" is a question with an answer rather than an
investigation.

Each environment gets its own checkout of UrbanLens *and* REData, its own
containers (compose project name), its own database, and a hostname of
``<slug>.dev.urbanlens.org``. Nothing here talks to Nginx Proxy Manager: the
dev router (see ``bin/opslib/router.py``) matches on the Host header, so NPM
needs one wildcard entry, once, rather than an edit per environment.

Stdlib only - this has to run on a host where the project venv may not exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .steps import Run

#: Where environments live, one directory per slug.
DEFAULT_ROOT = Path(os.getenv("UL_DEV_ROOT", "/projects/environments/agents"))

#: Registry of live environments. A file rather than "look at docker" because
#: it records intent - a stopped environment is still allocated, and its ports
#: must not be handed to somebody else.
REGISTRY = Path(os.getenv("UL_DEV_REGISTRY", str(DEFAULT_ROOT / "registry.json")))

#: Hostname suffix. One wildcard DNS record and one wildcard proxy entry cover
#: every environment ever created under it.
DOMAIN_SUFFIX = os.getenv("UL_DEV_DOMAIN_SUFFIX", "dev.urbanlens.org")

#: Port block per environment. Chosen high and contiguous so one environment's
#: ports are obvious from its base, and collisions with the fixed s1/s2/s3
#: environments (21800-21899) are impossible by construction.
PORT_BASE = int(os.getenv("UL_DEV_PORT_BASE", "31000"))
PORT_STRIDE = 20
PORT_CEILING = 39000

#: Where each repository is cloned from. Defaults to the checkout already on
#: this host when there is one: cloning locally needs no credentials, takes
#: seconds rather than minutes, and cannot be rate-limited - and `git clone`
#: from a local path still produces an independent repository with the real
#: remote inherited, so `git fetch origin` in the new environment works.
def _clone_source(local: str, remote: str) -> str:
    """Prefer a local checkout, falling back to the remote."""
    return local if Path(local, ".git").exists() else remote


REPOS = {
    "urbanlens": os.getenv("UL_DEV_UL_REMOTE", "") or _clone_source("/projects/UrbanLens/UrbanLens", "https://github.com/UrbanLens/UrbanLens.git"),
    "redata": os.getenv("UL_DEV_RD_REMOTE", "") or _clone_source("/projects/UrbanLens/REData", "https://github.com/UrbanLens/REData.git"),
}


@dataclass
class DevEnv:
    """One allocated environment."""

    slug: str
    path: str
    hostname: str
    url: str
    app_port: int
    redata_port: int
    created_at: str
    branch: str
    owner: str = ""
    status: str = "creating"
    #: Credentials for the seeded account (see `_seed_demo_account`). Recorded
    #: rather than only printed once, so `list` can answer "how do I get into
    #: this one" later. Not a secret worth hiding: the password is derived from
    #: the slug, which is in the hostname.
    login_username: str = ""
    login_password: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return asdict(self)


def _load_registry() -> dict[str, dict[str, Any]]:
    """Every allocated environment, keyed by slug."""
    if not REGISTRY.is_file():
        return {}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_registry(entries: dict[str, dict[str, Any]]) -> None:
    """Persist the registry, creating its directory if needed."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")


def _port_free(port: int) -> bool:
    """Whether a TCP port can be bound on all interfaces right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # All interfaces on purpose: docker publishes on 0.0.0.0, so a
            # loopback-only probe would report a port free that docker then
            # fails to bind. Nothing is served here - the socket is closed
            # immediately.
            probe.bind(("0.0.0.0", port))  # noqa: S104
        except OSError:
            return False
    return True


def allocate_ports(entries: dict[str, dict[str, Any]]) -> int:
    """Find a free port block base.

    Checked against both the registry and the live socket table: the registry
    catches an environment that is merely stopped, and the socket check catches
    anything on this host that was never registered here at all.

    Args:
        entries: The current registry.

    Returns:
        The base port of a free block.

    Raises:
        RuntimeError: No block is available below the ceiling.
    """
    taken = {int(entry["app_port"]) for entry in entries.values() if entry.get("app_port")}
    for base in range(PORT_BASE, PORT_CEILING, PORT_STRIDE):
        if base in taken:
            continue
        if all(_port_free(base + offset) for offset in (0, 1)):
            return base
    raise RuntimeError(f"no free port block between {PORT_BASE} and {PORT_CEILING}")


def generate_slug(entries: dict[str, dict[str, Any]], requested: str = "") -> str:
    """A short, unique, DNS-safe name for an environment.

    Args:
        entries: The current registry, for collision checks.
        requested: A preferred name; sanitised and used when free.

    Returns:
        The slug.

    Raises:
        ValueError: A requested name is already taken.
    """
    if requested:
        slug = re.sub(r"[^a-z0-9-]", "-", requested.lower()).strip("-")[:24]
        if not slug:
            raise ValueError(f"{requested!r} contains no usable characters")
        if slug in entries:
            raise ValueError(f"environment {slug!r} already exists")
        return slug
    while True:
        slug = f"a{secrets.token_hex(3)}"
        if slug not in entries:
            return slug


def _write_env_file(path: Path, values: dict[str, str], source: Path | None) -> None:
    """Write an environment file, seeded from a source .env when present.

    Secrets (API keys, OAuth credentials) are inherited from the host's own
    environment file rather than invented, so a dev environment can actually
    reach the services it integrates with. Keys the caller sets explicitly win.

    Args:
        path: Destination .env.
        values: Overrides that make this environment distinct.
        source: An existing .env to inherit everything else from.
    """
    lines: list[str] = []
    seen: set[str] = set()
    if source and source.is_file():
        for line in source.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key in values:
                continue
            lines.append(line)
            seen.add(key)
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


#: Services whose container_name the base compose file pins. Anything not
#: listed simply keeps compose's own project-prefixed name, which is already
#: unique per project.
_NAMED_SERVICES = ("app", "app-ws", "nginx", "db", "valkey", "celery-worker", "celery-worker-panels", "celery-beat", "clamav")

#: Services that run the full migration set on first boot.
_SLOW_FIRST_BOOT_SERVICES = ("app", "app-ws")

#: Services running application code, and so the ones that make outbound calls
#: to this environment's own REData instance.
_APP_SERVICES = ("app", "app-ws", "celery-worker", "celery-worker-panels", "celery-beat")

#: How a container reaches a port published on the host. REData publishes its
#: app port on all interfaces, but from inside a container ``127.0.0.1`` is the
#: container - so the environment's ``UL_REDATA_API_URL`` resolved to nothing at
#: all, and every REData call from the app failed while the same URL worked
#: perfectly when tested from a shell on the host. Docker resolves this alias to
#: the host only when the service asks for it, hence the ``extra_hosts`` entry
#: the override writes alongside it.
_HOST_ALIAS = "host.docker.internal"


def redata_api_url(port: int | None) -> str:
    """Where this environment's app containers reach its own REData.

    Args:
        port: The host port REData publishes on, or None when this environment
            has no REData of its own.

    Returns:
        The URL for ``UL_REDATA_API_URL``, or ``""`` when there is no REData.
        Never ``127.0.0.1``: that is the *app* container from inside the app
        container, so the setting resolved to nothing while testing identically
        from a shell on the host, which is where anyone would test it.
    """
    return f"http://{_HOST_ALIAS}:{port}" if port else ""


def redata_allowed_hosts() -> str:
    """``RD_ALLOWED_HOSTS`` for an environment's own REData.

    Returns:
        The comma-separated hosts REData will answer to. Must include whatever
        host :func:`redata_api_url` names, or Django answers 400 DisallowedHost
        to every call - a *worse* failure than the connection-refused it
        replaced, because a 400 from a service that is plainly up reads as a bad
        request rather than as a misconfigured address.
    """
    return f"localhost,127.0.0.1,{_HOST_ALIAS}"

#: Grace before an unhealthy verdict on first boot. Generous on purpose: the
#: cost of waiting is a slower create, while the cost of being too strict is a
#: torn-down stack that was working.
_FIRST_BOOT_GRACE = "20m"


def container_name(slug: str, service: str) -> str:
    """The container name this tool pins for one service of one environment.

    Derived in one place because three sites need it and they had already
    drifted: the isolation override writes ``ul_<slug>_<service>``, the log
    capture asks for ``ul_<slug>_app``, and ``list_envs`` looked for
    ``agent_<slug>`` - a prefix nothing creates, so every environment was
    reported as not running.

    Args:
        slug: The environment slug.
        service: The compose service name (``app``, ``db``, ``app-ws``, ...).

    Returns:
        The pinned container name.
    """
    return f"ul_{slug}_{service.replace('-', '_')}"


def _app_container_exists(slug: str) -> bool:
    """Whether this environment's app container exists, running or not.

    Asked when ``docker compose up`` exits non-zero, to tell "nothing started"
    apart from "something started and a dependency's healthcheck ran out of
    patience" - which on a cold build is the normal case, not a failure.

    Args:
        slug: The environment slug.

    Returns:
        True when a container by that name exists.
    """
    import subprocess

    try:
        completed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{container_name(slug, 'app')}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return container_name(slug, "app") in completed.stdout


def _compose(slug: str) -> list[str]:
    """The compose command for one environment, isolated by project and override."""
    return ["docker", "compose", "-p", f"ul-{slug}", "-f", "docker-compose.yml", "-f", "docker-compose.agent.yml"]


def _isolation_override(slug: str, ul_dir: Path) -> str:
    """Compose override pinning every container name to this environment.

    Args:
        slug: Environment slug.
        ul_dir: The environment's UrbanLens checkout, read to find which
            services the base file actually defines.

    Returns:
        The override file's contents.
    """
    base = (ul_dir / "docker-compose.yml").read_text(encoding="utf-8")
    lines = [
        "# Generated per environment - do not edit.",
        "#",
        "# Pins container names so this stack cannot collide with another on the",
        "# host. Not left to UL_CONTAINER_NAME: that variable does not exist on every",
        "# branch, and where it is absent the names fall back to UL_ENVIRONMENT and",
        "# collide with the host's own development stack.",
        "#",
        "# Also gives the application services a route to the host, so this stack's",
        f"# own REData (published on a host port) is reachable at {_HOST_ALIAS}",
        "# rather than at 127.0.0.1, which inside a container is the container.",
        "services:",
    ]
    for service in _NAMED_SERVICES:
        if f"\n  {service}:" not in base:
            continue
        lines.append(f"  {service}:")
        lines.append(f"    container_name: {container_name(slug, service)}")
        if service in _APP_SERVICES:
            lines.append("    extra_hosts:")
            lines.append(f'      - "{_HOST_ALIAS}:host-gateway"')
        if service in _SLOW_FIRST_BOOT_SERVICES and "healthcheck:" in base:
            # A new environment migrates from an *empty* database - every
            # migration in the project, not the handful an existing deployment
            # applies. The base timings are sized for the latter (30s grace,
            # three 30s retries), so compose declares the app unhealthy while
            # it is still working and tears the dependent services down. The
            # container was fine; only the deadline was wrong.
            lines.append("    healthcheck:")
            lines.append(f"      start_period: {_FIRST_BOOT_GRACE}")
    return "\n".join(lines) + "\n"


#: Mints a key in this environment's own REData and prints it. Run through
#: ``manage.py shell -c`` rather than a management command because REData has
#: no key-issuing command - the key is normally created through the admin, which
#: no unattended flow can reach.
_MINT_KEY_SCRIPT = """
from django.contrib.auth.models import User
from redata.api.services.api_keys import generate_api_key
user, _ = User.objects.get_or_create(username="urbanlens-dev", defaults={"is_active": True})
_, raw = generate_api_key(user, "UrbanLens dev environment")
print("UL_REDATA_KEY=" + raw)
"""

#: Where the key line is printed, so the surrounding shell banner ("90 objects
#: imported automatically") does not have to be parsed around.
_MINT_KEY_MARKER = "UL_REDATA_KEY="


def _provision_redata_key(run: Run, slug: str, ul_dir: Path) -> None:
    """Give this environment's UrbanLens a key its *own* REData will accept.

    The third half of "the app cannot reach REData", and the one that survives
    fixing the other two. Secrets are seeded from the host's ``.env`` so a dev
    environment can reach the integrations it exercises - but
    ``UL_REDATA_API_KEY`` is not that kind of secret. It names a row in a
    *database*, and a private REData starts with an empty one, so the inherited
    key authenticates against nothing and every call comes back 401 - from a
    service that is up, answering, and reachable.

    Written into the UrbanLens ``.env`` before its stack starts, so nothing has
    to be restarted afterwards. Best-effort: a failure here leaves the
    environment usable for everything that is not REData, which is most of it,
    and says so in the step log rather than aborting a build that otherwise
    worked.

    Args:
        run: The step recorder for this create.
        slug: Environment slug (also the REData compose project suffix).
        ul_dir: The environment's UrbanLens checkout, holding the ``.env``.
    """
    container = f"redata-{slug}-app-1"
    result = subprocess.run(
        ["docker", "exec", container, "/app/.venv/bin/python", "/app/src/redata/manage.py", "shell", "-c", _MINT_KEY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    key = next((line.split("=", 1)[1].strip() for line in result.stdout.splitlines() if line.startswith(_MINT_KEY_MARKER)), "")
    if not key:
        run.record("provision-redata-key", "failed", f"could not mint a key in {container}: {(result.stderr or result.stdout).strip()[:300]}")
        return

    env_path = ul_dir / ".env"
    lines = [line for line in env_path.read_text(encoding="utf-8").splitlines() if not line.startswith("UL_REDATA_API_KEY=")]
    lines.append(f"UL_REDATA_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run.record("provision-redata-key", "ok", "minted a key in this environment's own REData")


#: Run inside the environment's own app container, which is the only place the
#: project's Python, its GDAL and its database all exist together. Credentials
#: arrive as environment variables rather than interpolated into the source, so
#: nothing here has to be quoted correctly to be correct.
_SEED_SCRIPT = """
import json
import os

from urbanlens.dashboard.services.demo.seeding import seed_dev_environment

summary = seed_dev_environment(username=os.environ["UL_DEV_SEED_USERNAME"], password=os.environ["UL_DEV_SEED_PASSWORD"])
print("UL_DEV_SEED=" + json.dumps(summary))
"""

#: Where the summary is printed, so the shell's own banner does not have to be
#: parsed around - same reason as `_MINT_KEY_MARKER`.
_SEED_MARKER = "UL_DEV_SEED="


def seed_password(slug: str) -> str:
    """The seeded account's password for one environment.

    Derived from the slug rather than random so that whoever created the
    environment - or anyone reading its hostname later - can work it out without
    finding the run record.

    Args:
        slug: The environment slug.

    Returns:
        The password.
    """
    return f"demo-{slug}"


def _seed_demo_account(run: Run, env: DevEnv) -> None:
    """Give the environment an account with content, and report its credentials.

    A fresh environment's database is empty, so every page it serves is an empty
    state: there is nothing to look at and no way in without creating an account
    by hand first. This seeds the same content the public demo instance gets,
    under a fixed username, and puts the credentials in the record the caller
    already reads.

    Best-effort by construction. The content pool comes from a catalog that a
    brand-new environment may not be able to reach, and an environment with a
    login but no pins is still a working environment - so a failure here is
    recorded and the create continues, rather than discarding a stack that took
    forty minutes to build.

    Args:
        run: The step recorder for this create.
        env: The environment being created; its credentials are written back
            onto it.
    """
    username, password = "demo", seed_password(env.slug)
    result = run.run_step(
        "seed-demo-account",
        [
            "docker",
            "exec",
            "-e",
            f"UL_DEV_SEED_USERNAME={username}",
            "-e",
            f"UL_DEV_SEED_PASSWORD={password}",
            container_name(env.slug, "app"),
            "/app/.venv/bin/python",
            "/app/src/urbanlens/manage.py",
            "shell",
            "-c",
            _SEED_SCRIPT,
        ],
        timeout=1800,
        check=False,
    )
    summary = _parse_seed_summary(result.output_tail)
    if not summary:
        result.status = "warn"
        result.detail = f"no account was seeded ({result.detail or 'the seeder printed no summary'}); sign-in needs an account created by hand"
        return

    env.login_username, env.login_password = username, password
    result.detail = f"{username} / {password} - {summary.get('pins', 0)} pin(s), landmark {summary.get('landmark', 'none')}; {summary.get('catalog', '')}"


def _parse_seed_summary(output: str) -> dict[str, Any]:
    """The seeder's JSON summary, or an empty dict when it did not produce one.

    Args:
        output: Captured stdout/stderr of the seeding step.

    Returns:
        The parsed summary. Empty when the marker line is absent or unparseable
        - which is what "the account was not seeded" looks like from here.
    """
    for line in output.splitlines():
        if line.startswith(_SEED_MARKER):
            try:
                parsed = json.loads(line[len(_SEED_MARKER) :])
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) and parsed.get("created") else {}
    return {}


def _naming_conflict(slug: str, ul_dir: Path) -> str:
    """Whether this environment's resolved container names belong to another stack.

    The guard that was missing: a create that recreates somebody else's
    containers looks like a successful build right up until their environment
    stops working.

    Args:
        slug: Environment slug.
        ul_dir: The environment's UrbanLens checkout.

    Returns:
        A description of the conflict, or "" when there is none.
    """
    try:
        resolved = subprocess.run([*_compose(slug), "config", "--format", "json"], cwd=ul_dir, capture_output=True, text=True, timeout=180, check=False)
        services = json.loads(resolved.stdout or "{}").get("services", {})
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return f"could not resolve compose config: {exc}"

    wanted = {config.get("container_name") for config in services.values() if config.get("container_name")}
    if not wanted:
        return ""

    try:
        listed = subprocess.run(["docker", "ps", "-a", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project.working_dir"}}'], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not list containers: {exc}"

    return _conflicting_name(wanted, listed.stdout, ul_dir)


def _conflicting_name(wanted: set[str], docker_listing: str, ul_dir: Path) -> str:
    """Find a wanted container name that belongs to a different directory.

    Split out from the docker call so the decision itself can be tested: this
    is the check whose absence let a create recreate the host's existing
    development stack.

    Args:
        wanted: Container names this environment intends to create.
        docker_listing: ``docker ps -a`` output, one ``name<TAB>working_dir``
            per line.
        ul_dir: This environment's own checkout - containers already owned by
            it are ours to recreate.

    Returns:
        A description of the first conflict, or "" when there is none.
    """
    for line in docker_listing.splitlines():
        name, _, owner = line.partition("\t")
        if name not in wanted:
            continue
        if owner and Path(owner) == ul_dir:
            # Ours already - recreating it is what a repeat create does.
            continue
        # Anything else with this name blocks us, owner label or not: docker
        # container names are global, so `up` fails with "name already in use"
        # whatever project it belongs to. Refusing here says whose it is;
        # letting compose fail says only that the name is taken.
        return f"container {name!r} already exists and belongs to {owner or 'an unknown owner'}"
    return ""


def _clone_command(source: str, branch: str, destination: Path) -> list[str]:
    """Build the clone command for one repository.

    ``--depth`` is deliberately omitted for a local source: git ignores it
    there (and says so), while a plain local clone hardlinks its objects - so
    it is already faster and uses less disk than a shallow network clone would.

    Args:
        source: Local path or remote URL.
        branch: Branch to check out, or empty for the source's default.
        destination: Where to clone to.

    Returns:
        The argv to run.
    """
    command = ["git", "clone"]
    if branch:
        command += ["--branch", branch]
    if not Path(source, ".git").exists():
        command += ["--depth", "50"]
    return [*command, source, str(destination)]


def create(*, requested_name: str = "", branch: str = "main", owner: str = "", root: Path | None = None, run_dir: Path | None = None, with_redata: bool = True) -> dict[str, Any]:
    """Create a fresh dev environment and bring it up.

    Args:
        requested_name: Preferred slug; a random one is generated when empty.
        branch: Branch to check out in both repositories.
        owner: Free-text note of who or what asked for it, shown by ``list``.
        root: Parent directory for environments.
        run_dir: Where the run log and record are written.
        with_redata: Also create and start a REData instance for this
            environment. UrbanLens is pointed at it automatically.

    Returns:
        The run record, with the environment under ``context.env``.
    """
    from .steps import new_run

    root = root or DEFAULT_ROOT
    run = new_run("dev-create", run_dir or (root / ".ops-runs"))

    entries = _load_registry()
    try:
        slug = generate_slug(entries, requested_name)
        base_port = allocate_ports(entries)
    except (ValueError, RuntimeError) as exc:
        run.record("allocate", "failed", str(exc))
        return run.finish()

    env = DevEnv(
        slug=slug,
        path=str(root / slug),
        hostname=f"{slug}.{DOMAIN_SUFFIX}",
        url=f"https://{slug}.{DOMAIN_SUFFIX}",
        app_port=base_port,
        redata_port=base_port + 1,
        created_at=datetime.now(UTC).isoformat(),
        branch=branch,
        owner=owner,
    )
    run.context["env"] = env.as_dict()
    run.record("allocate", "ok", f"{slug} -> app {base_port}, redata {base_port + 1}")

    # Registered before anything is built, so a failure part-way still holds
    # the ports and the name - a half-built environment that another caller can
    # allocate over is worse than one that needs destroying.
    entries[slug] = env.as_dict()
    _save_registry(entries)

    from .router import ensure_router

    router_state = ensure_router()
    run.record("ensure-router", "ok" if router_state.get("router") != "failed" else "warn", json.dumps(router_state))

    env_path = Path(env.path)
    env_path.mkdir(parents=True, exist_ok=True)

    def _abandon(reason: str) -> dict:
        """Mark the reserved entry failed and stop, instead of building on nothing.

        `run_step` records a failure rather than raising, so without this the
        next step read a directory the clone never created and `create` died on
        an uncaught traceback - having already written a registry entry that
        claimed the environment existed.
        """
        run.record("abort", "failed", reason)
        current = _load_registry()
        current[slug] = {**env.as_dict(), "status": "failed"}
        _save_registry(current)
        return run.finish()

    ul_dir = env_path / "UrbanLens"
    run.run_step("clone-urbanlens", _clone_command(REPOS["urbanlens"], branch, ul_dir), timeout=1800)
    if run.steps[-1].status == "failed" or not ul_dir.is_dir():
        return _abandon(f"clone of UrbanLens@{branch or 'default'} failed; nothing to build in {ul_dir}")

    rd_dir = env_path / "REData"
    if with_redata:
        run.run_step("clone-redata", _clone_command(REPOS["redata"], "", rd_dir), timeout=1800)
        if run.steps[-1].status == "failed" or not rd_dir.is_dir():
            return _abandon(f"clone of REData failed; nothing to build in {rd_dir}")

    # Secrets are inherited from whichever checkout this host already runs, so
    # a dev environment can reach the integrations it is meant to exercise.
    source_env = Path(os.getenv("UL_DEV_SEED_ENV", "/projects/UrbanLens/UrbanLens/.env"))
    _write_env_file(
        ul_dir / ".env",
        {
            # "development" is what makes init.py run Django's runserver with
            # its autoreloader rather than gunicorn - which is the half of hot
            # reload that does not need a bind mount.
            "UL_ENVIRONMENT": "development",
            # Does NOT name these containers - the isolation override does, via
            # `container_name()`. Kept because the base compose file interpolates
            # it into its own `name:` and `container_name:` templates, and an
            # unset variable there falls back to UL_ENVIRONMENT ("development"),
            # which is the host's own stack. It is a collision guard, not an
            # identifier: do not derive a container name from it.
            "UL_CONTAINER_NAME": f"agent_{slug}",
            "UL_APP_PORT": str(env.app_port),
            "UL_DB_NAME": f"urbanlens_{slug}",
            "UL_SITE_URL": env.url,
            # Without this Django answers 400 to its own hostname, which looks
            # exactly like a broken proxy: the router forwards correctly and
            # the app rejects the Host header it was just given. Localhost is
            # included so the published port is directly usable too, which is
            # what the health check and any port-forwarded debugging rely on.
            "UL_ALLOWED_HOSTS": f"{env.hostname},127.0.0.1,localhost",
            # Routed to the host by the `extra_hosts` entry the isolation
            # override writes for the application services.
            "UL_REDATA_API_URL": redata_api_url(env.redata_port if with_redata else None),
            # Where the seeded location catalog is recorded, so a later
            # `import_public_locations`/`import_redata_public_locations` in this
            # environment tops the same manifest up rather than starting a
            # second one. /app is the app user's own tree (see the Dockerfile's
            # COPY --chown), and is what .env-sample suggests for this.
            "UL_DEMO_LOCATIONS_FILE": "/app/demo-locations.json",
        },
        source_env if source_env.is_file() else None,
    )
    run.record("write-env", "ok", f"{ul_dir / '.env'}")

    # Container names are pinned by an override rather than by UL_CONTAINER_NAME.
    # That variable exists only on some branches: on `main` the compose file
    # names everything after UL_ENVIRONMENT alone, so an environment asking for
    # `development` (which is what enables the autoreloader) resolves to the
    # *same container names* as the host's existing development stack and
    # recreates it. An override is branch-independent, because it does not care
    # what the file interpolates.
    (ul_dir / "docker-compose.agent.yml").write_text(_isolation_override(slug, ul_dir), encoding="utf-8")
    run.record("write-isolation-override", "ok", "container names pinned to this environment")

    conflict = _naming_conflict(slug, ul_dir)
    if conflict:
        run.record("check-name-collision", "failed", conflict)
        entries = _load_registry()
        entries[slug] = {**env.as_dict(), "status": "failed"}
        _save_registry(entries)
        return run.finish()
    run.record("check-name-collision", "ok", "no container names belong to another stack")

    if with_redata and rd_dir.is_dir():
        rd_source = Path(os.getenv("UL_DEV_SEED_RD_ENV", "/projects/UrbanLens/REData/.env"))
        # RD_ENVIRONMENT is a fixed five-value enum that settings branch on, so
        # it cannot double as a per-instance discriminator the way UrbanLens's
        # UL_CONTAINER_NAME does. COMPOSE_PROJECT_NAME (passed as -p below) is
        # what keeps parallel instances apart.
        _write_env_file(
            rd_dir / ".env",
            {
                "RD_ENVIRONMENT": "local",
                "RD_APP_PORT": str(env.redata_port),
                # Verified 2026-08-20: a container reaching this port over the
                # host gateway got 400 where the host itself got 401, because
                # the seed .env allows only localhost/127.0.0.1.
                "RD_ALLOWED_HOSTS": redata_allowed_hosts(),
            },
            rd_source if rd_source.is_file() else None,
        )
        redata_started = run.run_step("start-redata", ["docker", "compose", "-p", f"redata-{slug}", "up", "-d"], cwd=rd_dir, timeout=3600, check=False)
        if redata_started.status == "ok":
            _provision_redata_key(run, slug, ul_dir)

    # `_compose` passes `-p ul-<slug>`, and the override file pins every
    # `container_name`. Both deliberately *override* what the base compose file
    # would derive from UL_CONTAINER_NAME, because that derivation is
    # `urbanlens_<name>_<service>` and cannot produce the `ul_<slug>_<service>`
    # form this tool needs. So the names come from exactly two places - the -p
    # here and `container_name()` - and from nowhere else. An earlier version of
    # this comment claimed the opposite ("No -p: UL_CONTAINER_NAME ... already
    # sets both"), which is how `list_envs` came to look for a prefix nothing
    # creates and reported every environment as stopped.
    # check=False so a slow first boot can be told apart from a real failure
    # below, rather than ending the run before anything has been asked.
    started = run.run_step("start-urbanlens", [*_compose(slug), "up", "--build", "-d"], cwd=ul_dir, timeout=5400, check=False)

    # `up -d` exits non-zero when a dependent service gives up waiting on the
    # app's healthcheck - and on a cold first boot the app legitimately takes
    # minutes to pass it, because it migrates, collectstatics and builds the
    # frontend before binding a port. The stack that produced this comment came
    # up perfectly ninety seconds after `up` reported "dependency failed to
    # start: container ul_<slug>_app is unhealthy", and the fifteen-minute wait
    # that would have seen that was skipped precisely because the start had
    # "failed". So: if the container exists, wait for it. Only when there is no
    # container at all is there nothing to wait for.
    if started.status == "ok" or _app_container_exists(slug):
        if started.status != "ok":
            run.record("start-urbanlens-slow", "warn", "compose gave up on a dependency healthcheck, but the app container exists - waiting for it rather than abandoning the run")
        healthy = run.run_check("wait-healthy", lambda: _app_answers(env.app_port), timeout=900, interval=5)
        if healthy.status != "ok":
            run.record("capture-app-log-detail", "ok", "app never answered; its log follows")
            run.run_step("capture-app-log", ["docker", "logs", "--tail", "60", container_name(slug, "app")], cwd=ul_dir, timeout=180, check=False)
        else:
            # Only once the app answers: the entrypoint runs migrations before
            # it binds a port, so this is also the first moment the schema the
            # seeder writes into actually exists.
            _seed_demo_account(run, env)
    else:
        # Waiting fifteen minutes for a container that already failed to start
        # is fifteen minutes of nothing. Report why instead: the app's own log
        # holds the reason, and fetching it here is the difference between a
        # caller knowing what broke and a caller going to find out.
        run.record("wait-healthy", "skipped", "start failed; not waiting on a container that did not start")
        run.record("seed-demo-account", "skipped", "no running app to seed into")
        # `docker logs <container>` rather than `docker compose logs`: compose
        # prefixes its own variable-substitution warnings, and on this project
        # there are enough of them to push the app's actual traceback out of
        # any reasonable tail - which defeats the point of capturing it.
        run.run_step("capture-app-log", ["docker", "logs", "--tail", "60", container_name(slug, "app")], cwd=ul_dir, timeout=180, check=False)

    from .router import write_routes

    routes_written = write_routes(_load_registry())
    run.record("publish-route", "ok" if routes_written else "warn", f"{env.hostname} -> 127.0.0.1:{env.app_port}")

    entries = _load_registry()
    env.status = "failed" if run.failed else "ready"
    entries[slug] = env.as_dict()
    _save_registry(entries)
    run.context["env"] = env.as_dict()
    return run.finish()


def _app_answers(port: int) -> tuple[bool, str]:
    """Whether the app is serving on a port yet."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=8) as response:
            return response.status < 500, f"GET / -> {response.status}"
    except urllib.error.HTTPError as exc:
        return exc.code < 500, f"GET / -> {exc.code}"


def destroy(slug: str, *, root: Path | None = None, keep_files: bool = False) -> dict[str, Any]:
    """Tear an environment down and release its name and ports.

    Args:
        slug: Environment to remove.
        root: Parent directory for environments.
        keep_files: Leave the checkout on disk (containers and volumes still go).

    Returns:
        A summary of what was removed.
    """
    root = root or DEFAULT_ROOT
    entries = _load_registry()
    entry = entries.get(slug)
    env_path = Path(entry["path"]) if entry else root / slug

    removed = {"slug": slug, "containers": False, "files": False, "registry": False}

    # UrbanLens resolves its own project name from the .env still sitting in
    # the checkout; REData needs the -p it was created with.
    # Torn down with the same invocation each was created with, override file
    # included - a `down` that resolves a different project name leaves the
    # containers running and reports success.
    teardowns = ((_compose(slug), env_path / "UrbanLens"), (["docker", "compose", "-p", f"redata-{slug}"], env_path / "REData"))
    teardown_errors: list[str] = []
    attempted = False
    for base_command, cwd in teardowns:
        if not cwd.is_dir():
            continue
        attempted = True
        # -v removes the environment's volumes too. These are disposable by
        # construction, and leaving them behind is how a host fills up.
        completed = subprocess.run([*base_command, "down", "-v", "--remove-orphans"], cwd=cwd, capture_output=True, text=True, timeout=900, check=False)
        if completed.returncode != 0:
            teardown_errors.append(f"{' '.join(base_command)}: {(completed.stderr or completed.stdout or '').strip()[-400:]}")
    # Reported from the exit codes, not assumed. This used to be set to True
    # unconditionally, so a `down` that left every container running still said
    # the environment had been destroyed - and the registry entry was deleted
    # regardless, which is how a host ends up with containers nothing knows about.
    removed["containers"] = attempted and not teardown_errors
    if teardown_errors:
        removed["errors"] = teardown_errors

    if not keep_files and env_path.is_dir() and env_path != root and not teardown_errors:
        shutil.rmtree(env_path, ignore_errors=True)
        removed["files"] = True

    if entry and not teardown_errors:
        entries.pop(slug, None)
        _save_registry(entries)
        removed["registry"] = True

        from .router import write_routes

        write_routes(entries)
    elif entry and teardown_errors:
        # Kept, and marked, rather than dropped: an entry is the only record
        # that these containers exist, and the whole point of noticing the
        # failure is that someone can come back to them.
        entries[slug] = {**entry, "status": "orphaned"}
        _save_registry(entries)
        removed["registry"] = False

    return removed


def list_envs() -> list[dict[str, Any]]:
    """Every allocated environment, with whether its containers are actually up.

    The registry records intent; this adds observed state, because the question
    behind "which environments are in use" is usually "which ones can I take".

    Returns:
        Registry entries, each with a ``running`` flag.
    """
    entries = _load_registry()
    try:
        completed = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60, check=False)
        running_names = completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        running_names = ""

    result = []
    for slug, entry in sorted(entries.items()):
        enriched = dict(entry)
        enriched["running"] = container_name(slug, "app") in running_names
        result.append(enriched)
    return result
