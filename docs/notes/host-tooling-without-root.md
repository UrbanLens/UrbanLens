# Host tooling without root

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R30 — mypy and pytest run on a host with no system GDAL, on the copies the pyogrio and shapely wheels vendor

`id: R30` · `status: current` · `updated: 2026-09-23`

Measured on chiron (Ubuntu 24.04, no `gdal-bin`, no passwordless sudo). `ldconfig -p | grep -iE
'gdal|geos|proj'` finds nothing there.

### GeoDjango on the host

`settings/_gdal_local.py` is imported by `settings/local.py` and `settings/test.py`. On POSIX, when
`ctypes.util.find_library` finds no system `gdal` or `geos_c`, it sets `GDAL_LIBRARY_PATH` and
`GEOS_LIBRARY_PATH` to the libraries vendored in `.venv/.../pyogrio.libs` and `shapely.libs`. It
also points `GDAL_DATA`/`PROJ_DATA` at pyogrio's data directories. The wheel's libgdal needs
nothing outside the base system (`ldd`: libc, libstdc++, libm and similar). The wheel's libgeos_c
does. It names its sibling libgeos
with no RUNPATH, so opening it straight fails with `libgeos-….so: cannot open shared object file`.
The helper therefore loads libgeos first, and the dynamic linker resolves the dependency by
soname. On Windows it keeps the earlier behaviour (DLLs from the same wheels, local env only).

Containers install `libgdal-dev`, so there `find_library` succeeds and the helper returns `{}`. Run
against the app container on 2026-09-23, it returned `{}`.

| | GDAL | GEOS |
|---|---|---|
| host (pyogrio 0.13.0 / shapely 2.1.2 wheels) | 3.12.4 | 3.13.1 |
| app image (`libgdal-dev`, Dockerfile) | 3.13.2 | 3.14.1 |
| Ubuntu 24.04 apt (`apt-cache policy`) | 3.8.4 | 3.12.1 |

`pyogrio~=0.13.0` is a direct dependency in `pyproject.toml` because its wheel now sets the host's
GDAL version. Bumping it moves the host's GDAL.

Verified from the host on 2026-09-23:

- `uv run python -m mypy src/urbanlens`: `Success: no issues found in 1145 source files`, 59s.
  Before the change, the django-stubs plugin died with `ImproperlyConfigured: Could not find the
  GDAL library`.
- `src/urbanlens/dashboard/tests/hypothesis/test_settings_gdal_local.py`: 3 passed on the host, and
  passed in the test-runner. One of the three loads the wheel libraries in a fresh interpreter and
  transforms a point 4326→3857.

`bin/setup_host_gis.sh` runs `uv sync` and then boots the test settings. It prints the host's
GDAL/GEOS next to the app container's. It is safe to rerun.

**With sudo**, `sudo apt install gdal-bin libgdal-dev` puts system libraries in place, and the
fallback steps aside. That gives GDAL 3.8.4, which is further from the image than the wheel
copy. It was not tried here.

**Not taken:** a micromamba/conda-forge install. It would match the image's version exactly, but
it downloads a second copy of the libraries and needs its own path wiring. The wheels are already
locked in `uv.lock`.

### pytest on the host

pytest also needs a PostGIS database. `bin/host_pytest.sh` (`bun run test:host`) points the run at
the dev stack's `test_db`, which shares the test-runner's network namespace and publishes no port.
It reads the test-runner's bridge IP and its `UL_DB_*` credentials from `docker inspect`, and it
refuses to start unless `UL_TEST_DB_NAME` is set.

```bash
UL_TEST_DB_NAME=test_<unique> bin/host_pytest.sh --reuse-db src/urbanlens/... -k ...
```

The tests' network guard (`core/testing_network.py`) allows only localhost. It patches Python's
`socket` module, and psycopg connects through libpq, below that layer, so the non-local database
IP is not blocked. Measured: 18 tests from three GIS files, two of them DB-backed, passed in 235s, most of it creating and
migrating the test database. The rerun with `--reuse-db` took 6s.

**Does not work on the host:** a test that opens its own Redis or Postgres socket from Python. The
guard blocks it, and nothing listens on localhost:6379. Run those with `bin/run_tests.sh`. The
test settings otherwise use locmem, an in-memory channel layer and an in-memory broker, so most
tests need no Redis. **Not measured:** the full suite on the host.

`docker exec <app> pytest` no longer works on a dev slot with per-tier roles (P130), so this and
`bin/run_tests.sh` are the two working routes.

### Frontend and package.json scripts

- **`bun run sass` does not crash.** It was switched to `bun node_modules/sass/sass.js` in
  57a4a90af. Measured: `bun run sass`, `npm run sass`, `node node_modules/sass/sass.js` and
  `node_modules/.bin/sass` all exit 0. `sass:dev` output is byte-identical to the documented
  workaround except for the `sourceMappingURL` comment, which `sass:dev` asks for. `sass:watch`
  recompiles on change. The original crash was not reproduced, so its cause is unknown.
- **`lint`** already ran ruff rather than pyright (2814580b7), but through a bare `ruff` that is
  not on PATH (exit 127). `lint`, `pre-commit`, `coverage` and `docs` now go through `uv run`.
  `lint` also runs mypy (53s warm). **Not run:** `bun run docs`, a build that takes tens of
  minutes. Only `uv run python -m sphinx --version` was checked.
- **Still broken, not changed here:** `migrate` joins its two commands with `&`, so `makemigrations`
  runs in the background alongside `migrate`. `start` and `db` assume a Python with the project's
  dependencies on PATH. `coverage` runs, but it fails on this checkout's stale `.coverage` data file, which
  names a deleted source file.
