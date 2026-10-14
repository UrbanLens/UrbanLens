# tests/integration

Playwright suite that drives a **deployed** UrbanLens instance. Run by hand
against staging; never against production, which the config refuses outright.

The helpers in `lib/` and the specs show how the accounts work and how to write a test.

## Run it

```bash
# On the deployment under test (for a Compose stack, inside the app container, then copy the file out):
python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json
docker cp urbanlens_<UL_CONTAINER_NAME>_app:/tmp/e2e.json /tmp/e2e.json

# Here:
UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json \
  ../../bin/run_integration_tests.sh --url http://localhost:21800
```

The suite creates far more through the external API than a person would, so the deployment under test needs its API
rate caps raised, or most fixtures fail with 429: set `UL_EXTERNAL_API_WRITE_RATE=5000/hour`,
`UL_EXTERNAL_API_READ_RATE=10000/hour` and `UL_EXTERNAL_API_BURST_RATE=600/minute` in its `.env`. Its Celery workers
must be running: wiki creation, photo processing and exports are asserted end to end. The manifest holds the test
accounts' credentials; delete it when the run is done.

`--docker` needs nothing installed but Docker. `--project smoke` is the
five-second version.

## Layout

```
lib/          the framework - fixtures, clients, page objects, helpers
setup/        preflight and the once-per-run sign-in
specs/
  smoke/      is it alive, does every page render
  services/   Valkey, Celery, Channels, static pipeline, CDNs, headers
  api/        the published external API contract
  ui/         real journeys in a real browser
  security/   authorisation, isolation, CSRF, disclosure, input handling
  a11y/       axe scans
  visual/     screenshot comparison (opt-in: UL_E2E_VISUAL=1)
```

Configuration is environment variables - see [`.env.example`](.env.example). A
`.env` beside it is read automatically and is gitignored, as are `.auth/`
(live session state) and `reports/`.
