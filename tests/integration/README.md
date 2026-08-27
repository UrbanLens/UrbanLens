# tests/integration

Playwright suite that drives a **deployed** UrbanLens instance. Run by hand
against staging; never against production, which the config refuses outright.

Full documentation, including why Playwright, how the accounts work and how to
write a test: **[`docs/INTEGRATION_TESTS.md`](../../docs/INTEGRATION_TESTS.md)**.

## Run it

```bash
# On the deployment under test:
python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json

# Here:
UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json \
  ../../bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org
```

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
  a11y/       axe scans
  visual/     screenshot comparison (opt-in: UL_E2E_VISUAL=1)
```

Configuration is environment variables - see [`.env.example`](.env.example). A
`.env` beside it is read automatically and is gitignored, as are `.auth/`
(live session state) and `reports/`.
