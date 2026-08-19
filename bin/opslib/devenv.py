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
from typing import Any

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
    lines = ["# Generated per environment - do not edit.", "#", "# Pins container names so this stack cannot collide with another on the", "# host. Not left to UL_CONTAINER_NAME: that variable does not exist on every", "# branch, and where it is absent the names fall back to UL_ENVIRONMENT and", "# collide with the host's own development stack.", "services:"]
    for service in _NAMED_SERVICES:
        if f"\n  {service}:" not in base:
            continue
        lines.append(f"  {service}:")
        lines.append(f"    container_name: ul_{slug}_{service.replace('-', '_')}")
    return "\n".join(lines) + "\n"


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

    ul_dir = env_path / "UrbanLens"
    run.run_step("clone-urbanlens", _clone_command(REPOS["urbanlens"], branch, ul_dir), timeout=1800)

    rd_dir = env_path / "REData"
    if with_redata:
        run.run_step("clone-redata", _clone_command(REPOS["redata"], "", rd_dir), timeout=1800)

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
            "UL_CONTAINER_NAME": f"agent_{slug}",
            "UL_APP_PORT": str(env.app_port),
            "UL_DB_NAME": f"urbanlens_{slug}",
            "UL_SITE_URL": env.url,
            "UL_REDATA_API_URL": f"http://127.0.0.1:{env.redata_port}" if with_redata else "",
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
            {"RD_ENVIRONMENT": "local", "RD_APP_PORT": str(env.redata_port)},
            rd_source if rd_source.is_file() else None,
        )
        run.run_step("start-redata", ["docker", "compose", "-p", f"redata-{slug}", "up", "-d"], cwd=rd_dir, timeout=3600, check=False)

    # No -p: UL_CONTAINER_NAME in the .env above already sets both the compose
    # project name and every container_name, so passing one here would create a
    # second, competing way to identify the same stack.
    run.run_step("start-urbanlens", [*_compose(slug), "up", "--build", "-d"], cwd=ul_dir, timeout=5400)

    run.run_check("wait-healthy", lambda: _app_answers(env.app_port), timeout=900, interval=5)

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
    for base_command, cwd in teardowns:
        if not cwd.is_dir():
            continue
        # -v removes the environment's volumes too. These are disposable by
        # construction, and leaving them behind is how a host fills up.
        subprocess.run([*base_command, "down", "-v", "--remove-orphans"], cwd=cwd, capture_output=True, text=True, timeout=900, check=False)
        removed["containers"] = True

    if not keep_files and env_path.is_dir() and env_path != root:
        shutil.rmtree(env_path, ignore_errors=True)
        removed["files"] = True

    if entry:
        entries.pop(slug, None)
        _save_registry(entries)
        removed["registry"] = True

        from .router import write_routes

        write_routes(entries)

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
        enriched["running"] = f"agent_{slug}" in running_names
        result.append(enriched)
    return result
