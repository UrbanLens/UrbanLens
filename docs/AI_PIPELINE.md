# The AI pipeline

How a chat message reaches a model, what the model is allowed to see and do
on the way, and what stops it from reaching REData, OAuth secrets, or the
open web while doing it.

## The threat this is built around

Not "the model is malicious" - it's a provider API UrbanLens pays for and
trusts to the same degree as any other vendor. The threat is what the model
is *reachable from* and what it's *handed*: a tool-calling loop executes
registry-dispatched handlers against arguments the model chose, over data
that includes user-authored text (comments, captions, trip names). A prompt
injection riding in on that text, or a bug in a handler, should not be able
to widen its reach past "this one profile's own data" - and the process
running the loop should not hold credentials (REData, OAuth, the media
encryption key's *use*, provider API keys for a vendor the loop didn't call)
it has no reason to touch.

The mitigation is the same shape as `docs/MEDIA_PIPELINE.md`'s: **placement**
(nothing that calls a provider or executes a tool runs in the web process),
and **narrow credentials per tier** (each container holds only what its job
requires, enforced by both the network topology and the code).

## The three tiers

```
app (web/websocket)  →  ai-worker (Celery, queue=ai)  →  ai-inference (Django-free)  →  provider
```

- **app** - gunicorn/daphne. Gates (`services.ai.access.assistant_available`),
  throttles, enqueues `run_assistant_turn_task`, renders. Never calls a
  provider and never executes a tool itself.
- **ai-worker** - a Celery worker (`--pool=prefork --concurrency=2`) draining
  only the `ai` queue. Runs the tool-calling loop, so it needs DB access and
  the field-encryption key (tools read encrypted content - the same residual
  as `media-worker`). It does **not** carry REData or OAuth credentials, or
  any provider API key. `UL_PROCESS_ROLE=ai` is hardcoded in its compose
  entry (not interpolated from the shared env), and
  `services.sandbox.guard.current_policy()` returns `DENY` unconditionally
  under `ProcessRole.AI` regardless of what `UL_UNTRUSTED_PARSE_POLICY`
  happens to be set to elsewhere - this worker never decodes untrusted bytes.
- **ai-inference** - a Django-free WSGI service (`urbanlens_ai/wsgi.py`,
  `gunicorn -k gthread`) on the same image as everything else, different
  entrypoint. Holds every provider API key (Anthropic, OpenAI, Cloudflare) and
  nothing else - no database, no cache, no secret key, no encryption key, no
  volumes. Normalizes each provider's API into one shape
  (`urbanlens_ai.schema.InferenceRequest`/`InferenceResponse`), and
  `urbanlens_ai.policy` rejects anything a request shouldn't be allowed to
  do before it reaches a provider SDK: an unlisted model, a provider
  server-side tool (web search, code execution - the one place "the model
  can't browse the web" is a mechanical guarantee instead of a prompt), or an
  over-cap `max_tokens`.

`ai-worker` talks to `ai-inference` over plain HTTP with a shared bearer
(`UL_AI_INFERENCE_TOKEN`) - `services.ai.inference_client.RemoteInferenceClient`
on the worker side, `urbanlens_ai.wsgi` on the inference side. This gives the
inference tier no outbound credential at all except the provider key it's
calling with: it validates a bearer, it never presents one anywhere else.
`services.ai.inference_client.get_inference_client()` picks
`RemoteInferenceClient` when `settings.ai_inference_url` is set, otherwise a
`LocalInferenceClient` that calls the same `urbanlens_ai.providers` code
in-process (local dev, tests) - so provider-call code exists in exactly one
place either way. `LocalInferenceClient` calls
`services.sandbox.guard.check_direct_inference()` first: outside
`ProcessRole.UNSPECIFIED`, an in-process provider call is a hard error under
the deployed `direct_inference_policy` (`deny` in staging/prod), so a
misconfigured deployment fails loudly instead of quietly routing real
inference through the wrong tier.

### The egress proxy

`egress-proxy` (tinyproxy, default-deny) is the **only** member of
`ai_egress_network` - the one network in this whole tier with a real gateway
to the internet. Every other network here is `internal: true`: no gateway, no
NAT, so the *only* route any of these containers has off its network is
through the proxy's allowlist - regardless of what a tool handler, a
misconfigured env var, or a future bug tries to reach. The allowlist is
exactly the hosts a shipped tool or provider adapter calls (provider API
hosts, OSRM, Open-Meteo/OpenWeatherMap). REData is not on it, deliberately -
see below.

The proxy reaches its clients over `proxy_network` and joins nothing else.
That separation is deliberate and worth keeping: the proxy is the one process
in this tier that parses bytes from the public internet, so it is the most
likely thing here to be compromised. Putting it on `ai_network` (db, valkey)
or `inference_network` (app, celery-worker) would mean a tinyproxy bug landed
an attacker on a network with a datastore on it - turning the component that
exists to *contain* egress into a route inward. `proxy_network` holds exactly
egress-proxy, ai-worker and ai-inference, so a foothold in the proxy reaches
only the two containers that were already allowed to call it.

| network | `internal` | members |
|---|---|---|
| `ai_network` | yes | db, valkey, ai-worker |
| `inference_network` | yes | app, celery-worker, ai-worker, ai-inference |
| `proxy_network` | yes | egress-proxy, ai-worker, ai-inference |
| `ai_egress_network` | **no** | egress-proxy |

One fragility to know about: a container's default route comes from the first
network it attaches to, and Compose orders equal-priority networks by name.
`ai_egress_network` sorts before `proxy_network`, which is what gives the
proxy its route out. Renaming either - or adding a third proxy network that
sorts earlier - would silently cut the proxy's egress, and every AI call with
it.

### Verifying the allowlist for real

The allowlist is the boundary, and a host missing from it fails at runtime
rather than at startup - `policy.validate_cloudflare_endpoint` will happily
accept a `*.cloudflare.com` endpoint the proxy then refuses. That already
happened once: the list carried `api.cloudflare.com` (the direct Workers AI
API) while the deployment's `UL_CLOUDFLARE_WORKER_AI_ENDPOINT` pointed at
`gateway.ai.cloudflare.com` (the AI Gateway in front of it), which silently
took out the default vision provider and the only image classifier.

`EgressFilterTests` pins the intended host set, but only a running tinyproxy
proves the regexes behave. That check needs no API keys, no database and no
existing environment - build the egress image and probe through it:

```sh
cd src/urbanlens/config/egress && docker build -t egress-check .
docker run -d --rm --name egress-check -p 127.0.0.1:38888:8888   --user tinyproxy:tinyproxy --read-only --cap-drop ALL   --security-opt no-new-privileges:true   --tmpfs /var/run/tinyproxy:size=1m,mode=1777 egress-check
# a numeric HTTP code means the proxy let the connection through to the real
# host; a curl error means tinyproxy refused it
for h in api.anthropic.com gateway.ai.cloudflare.com redata.urbanlens.org; do
  printf '%s ' "$h"
  curl -s -o /dev/null -w '%{http_code}
' --max-time 12 -x http://127.0.0.1:38888 "https://$h/" || echo DENIED
done
docker logs egress-check 2>&1 | grep 'Proxying refused'
docker rm -f egress-check
```

Include a look-alike such as `evil.cloudflare.com.attacker.net` - the entries
are anchored (`^...$`) precisely so a suffix match cannot slip past, and that
is worth re-proving whenever the filter changes. `FilterType ere` is also
load-bearing: it decides whether those lines are extended or basic regexes,
and the older `FilterExtended Yes` spelling is deprecated.

## "No REData, no web", enforced at three levels

1. **Env** - `ai-worker`'s compose entry doesn't carry `UL_REDATA_API_URL`/
   `UL_REDATA_API_KEY` or any OAuth secret. `ai-inference` doesn't carry a
   database URL, the encryption key, or REData/OAuth credentials either -
   only the provider keys it exists to hold.

   Those keys live in **`.env.ai`**, which only `ai-inference` reads
   (`env_file: .env.ai`; see `.env.ai-sample`). They are deliberately *not*
   in the root `.env` and are never referenced as `${UL_..._API_KEY}` in
   `docker-compose.yml`, because both would put them back on `app` and
   `celery-worker`: Compose resolves `${VAR}` from the root `.env`, and those
   two services load that whole file via `env_file`. A provider credential
   there would sit in the same process environment as `UL_DB_PASS` and
   `UL_FIELD_ENCRYPTION_KEY` - the adjacency this whole tier exists to break.
   `test_ai_isolation.py` asserts both halves (no provider key in any env
   anchor, no `${...}` interpolation of one anywhere in the compose file),
   and Django system check `dashboard.W001` warns at startup if a process
   that routes inference remotely can still read one.

   A local checkout running without Docker is the exception and is fine:
   `LocalInferenceClient` calls providers in-process and reads these from
   `AppSettings`, i.e. from the root `.env`. There is no container boundary
   there to protect, which is why `dashboard.W001` stays silent when
   `ai_inference_url` is unset.
2. **Code** - `services.apis.locations.redata_context_gateway.redata_configured()`
   returns `False` unconditionally when `current_role() is ProcessRole.AI`,
   so every REData-first resolution chokepoint (weather, routing, geocode,
   imagery, ...) takes its non-REData branch regardless of what credentials
   happen to be present. This is defense in depth, not the boundary - the
   real boundary is level 3. Two tools that need a live gateway at all,
   `distance_and_drive_time` and `get_weather`
   (`services/ai/tools/{routing,weather}.py`), bypass the REData-first
   chokepoints entirely and call `OSRMGateway`/`OpenWeatherMapGateway`/
   `OpenMeteoGateway` directly, with a module docstring saying so - each has
   a test asserting `redata_configured` (or the underlying gateway function)
   is never called. `has_tunnels` (`services/ai/tools/places.py`) is the
   sharpest example of the tradeoff: a `RedataUndergroundGateway` plugin
   exists and would answer "does this place have tunnels" far better than
   keyword-matching a floorplan/photos/comments, but using it would mean the
   AI worker depends on REData being reachable - so it isn't used, and the
   tool's own docstring explains why.
3. **Network** - even if a future tool tried a raw request to REData despite
   both guards above, `ai_network` has no route there: REData is not on
   `ai_egress_network`'s allowlist, and the network is `internal: true` with
   no other egress.

`tests/hypothesis/test_ai_isolation.py` checks all three mechanically: an
AST sweep of `services/ai/tools/` for forbidden imports (`redata_*`,
`*_resolution`, `requests`, `httpx`, `urllib`); a subprocess check that
importing `urbanlens_ai` never pulls in `django`/`urbanlens`; a PyYAML parse
of `docker-compose.yml` asserting the network topology and each AI service's
env keys are a subset of what's allowed; the role-guard functions themselves
(`current_policy()`, `redata_configured()`) under a patched `current_role()`;
and a check on `config/egress/filter` itself - no REData host ever allowed,
and every host a shipped provider adapter or tool gateway actually calls
present (`EgressFilterTests`). That last one exists because it's the one
gap a code review of the tools alone won't catch: `distance_and_drive_time`
and `get_weather` shipped once, in this same batch, without their hosts
(`router.project-osrm.org`, `api.open-meteo.com`, `api.openweathermap.org`)
ever being added to the filter - correct code, silently unreachable at
runtime, until this test made the omission a CI failure instead.

## The tool registry

`services/ai/tools/registry.py` defines `ToolSpec` (name, description,
pydantic `args_model`, `handler`, `read_only`, `features`,
`requires_external_apis`, `user_content_fields`, `scope`, `progress_label`,
`action_label`) and `ToolContext` (`profile`, `page`, `now`, `deadline`,
`dismissals`). One module per concern under `services/ai/tools/`: `pins`,
`trips`, `undo`, `routing`, `weather`, `places`, `visits`, `help`,
`dismissals` - each calls `register(ToolSpec(...))` at import time, and
`services/ai/tools/__init__.py` importing every module is the only discovery
mechanism, so a tool module not listed there silently doesn't exist.

`registry.execute()` enforces the rules below **generically**, so an
individual handler can't forget one:

- Unknown tool name or arguments that fail `args_model` validation → an
  error block back to the model, never an exception.
- Any string argument matching `https?://` → rejected before the handler
  runs. No tool takes a URL, so one appearing is the model trying to fetch
  something.
- Every field named in a tool's `user_content_fields` (comment snippets,
  photo captions, names) is truncated, run through
  `services.ai.scanner.scan()` (flagged, not silently dropped), and wrapped
  with `wrap_user_data()` - applied recursively, so it reaches strings
  nested inside lists of dicts (`has_tunnels`' `image_captions`/
  `comment_snippets`), not just top-level fields.
- The whole result is byte-capped (`MAX_TOOL_RESULT_CHARS`); an oversized
  result is discarded in favor of an error rather than truncated into
  invalid JSON.
- `features` gates a tool on `user_has_feature`; `requires_external_apis`
  drops routing/weather-shaped tools from the advertised list when
  `profile.external_apis_enabled` is off.
- Under `ProcessRole.AI`, any tool with `read_only=False` is refused
  outright - a write can only ever run through the confirm endpoint, on
  `app`, never inside the loop.

### Proposal/confirm for writes

A write tool (`create_trip`, `add_trip_activity`, `undo_last_action`) never
executes inside the loop. Calling it with `confirmed=False` builds a
proposal - `{tool, args, confirm_label}` from the *validated arguments*, not
from running the handler - and the reply carries a confirm button.
`POST /assistant/turn/<id>/confirm/<n>/` is the only place `execute(...,
confirmed=True)` runs, reading `args` back from a server-side cache keyed to
the turn and the caller's profile, never from anything the client sends.
`undo_last_action`'s proposal is bound to the `undo_uuid` `undo_peek`
returned; the confirm handler re-`peek_undo`s and compares that uuid against
the *current* top of the undo stack before restoring anything, so "undo
that" can't undo something the user did after asking and before confirming.

### `DataScope` and negative-access coverage

Every scoped tool declares `scope: DataScope.NONE | OWN_PROFILE |
VISIBLE_SHARED` - what kind of data its result could contain. Of the 14 tools
registered today, 11 have `scope != NONE` and each has a hand-written
negative-access test in its own test file (another profile's pin/trip/undo
entry/route/floorplan/photo/comment never surfaces).

`NEGATIVE_CASES` in `test_ai_tools_registry.py` is what keeps that true.
It maps every scoped tool to the tests covering it, and three checks run
against it:

- a tool registered with `scope != NONE` and no entry fails CI;
- an entry naming a tool that no longer exists fails CI;
- an entry naming a test that no longer exists fails CI - so the map cannot
  decay into a list of names that used to mean something.

The per-tool tests stay hand-written rather than generated. The 11 tools take
genuinely different arguments (`pin_slug`, `from_pin_slug`/`to_pin_slug`,
`undo_uuid`, trip `slug`, ...) and reach different models, so "what would a
leak even look like here" is a per-tool question and a uniform harness would
answer it badly. What is mechanized is that the question got asked - which is
the part a reviewer's memory was previously carrying.

## Grounded content

Two sources, neither of them the model's own knowledge:

- **Page help** (`services/ai/page_help.py`) - a static `PAGE_HELP` dict,
  `url_name → PageHelp(title, key_actions, tips)`, covering the primary-nav
  pages. `get_page_help()` returns an entry verbatim; the system prompt
  requires answering "how do I..." only from tool output.
- **Page context** (`services/ai/page_context.py`) - the client sends
  `location.pathname` (query string stripped server-side); `resolve_page_context()`
  runs `django.urls.resolve()` and a small resolver registry
  (`pin.details`, `trips.detail`, `map.view` today) that re-runs the same
  access check the real page's own view would - a spoofed or inaccessible
  path resolves to nothing, identically to an unsupported one, so a client
  can't tell the difference.
- **Dismissed explainers** - captured client-side, not server-registered:
  explainer ids are template-scoped and some are dynamic, so a Python
  registry would drift. `_page_explainer_script.html` and
  `onboarding-tour.ts` push `{id, kind, heading, body, page, at}` onto a
  capped sessionStorage ring (`ul_explainer_recent`) whenever the user
  dismisses one; it's sent with every turn, re-validated and re-capped
  server-side (`services/ai/dismissals.py`), and `recent_dismissals()`/
  `reopen_explainer(id)` answer only from that payload - the model can only
  ever quote text the user's own page actually rendered.

## Global surface

`installGlobalAssistantOverlay()` (`entries-classic/core.ts`) mounts a
`<dialog>`-based overlay on every page, reusing the same partials and
endpoints as the dedicated `/assistant/` page. The `openAssistant` hotkey
(`shift+?`/`?`, `frontend/ts/shared/hotkeys.ts`) opens it; a floating button
follows `undo-bar.ts`'s collision-avoiding placement pattern. Both are gated
on `assistant_available(profile)` - four checks in one place
(`services/ai/access.py`): the site-wide `UL_AI_WORKER_ENABLED` flag,
`SiteSettings.ai_enabled`, the profile's own `ai_enabled` and
`external_apis_enabled`, and `SiteFeature.AI` entitlement. An ungated user
gets no button, no hotkey binding, and a 404 from every turn endpoint.

## Async turn, everywhere

A turn is never answered synchronously from a web worker - both the HTMX
surface and the external API enqueue `run_assistant_turn_task` (queue `ai`,
`acks_late=False` - a turn that dies mid-loop must not be silently redelivered
and re-spend provider tokens on a bubble nobody is still waiting for) and
poll for the result. The task runs the loop under a monotonic deadline
(`ToolContext.deadline`); every tool that reaches an external gateway wraps
its call in `services.core.timeout_utils.call_with_deadline` using
`registry.remaining_deadline(context)`, so one slow provider can't blow past
the turn's overall budget. See `docs/EXTERNAL_API.md`'s "AI Assistant"
section for the wire-level request/response shapes on both surfaces.

## Vision and image classification

Photo keywording and the image classifier were the last provider calls made
outside this tier: `services/ai/vision.py` used to build an `OpenAI` client
and POST to Workers AI itself, on whichever worker ran the task. They now go
through the same `inference_client` seam as everything else, in two shapes:

- **Vision is an ordinary message.** A `Message.content` may be a list of
  parts instead of a bare string, and one of those parts can be an
  `ImagePart`. Each adapter translates it into its own provider's form -
  OpenAI's `image_url` with a `data:` URL, Anthropic's `{type: image,
  source: {type: base64}}` block, Cloudflare's flat `{image: [...], prompt}`
  payload (Workers AI vision models take no messages array at all). Vision
  *is* a chat completion, so it reuses `/v1/messages` rather than getting a
  parallel endpoint with its own auth, policy and adapter machinery.
- **Classification is not.** ResNet-50 takes no prompt, holds no
  conversation and spends no tokens; folding it into `InferenceRequest`
  would mean a request type where half the fields are meaningless. It gets
  `POST /v1/classify` and its own `ClassifyRequest`/`ClassifyResponse`.

Images cross the wire as inline base64, never a URL. A URL would be
something `ai-inference` has to *fetch*, which is exactly the capability the
egress allowlist exists to deny it - the caller reads the bytes and sends
them. `policy.MAX_IMAGE_BYTES` caps a single image at 1.5 MB *decoded* (not
base64 characters, which would let an image a third larger through), and
`MAX_IMAGES_PER_REQUEST` caps the count; callers send a 512px downscale, so
both are backstops rather than working limits.

Rate limiting, the service-key buckets and cost accounting stay in
`vision.py`, on the app side, for the same reason `LLMGateway` keeps its
own: `ai-inference` has no database and no idea what a service key is.

## Follow-ups (not yet done)

- **Read-only Postgres role for `ai-worker`**: the only write the loop
  performs today is `log_api_call`; moving that to a default-queue task
  would let `ai-worker`'s DB role be read-only.
- **MCP adapter**: not built. `ToolSpec` maps close to 1:1 onto MCP's `Tool`
  shape (name/description/inputSchema), so an adapter is a cheap follow-up
  if an external agent host (not just this app's own UI) ever needs to
  reach these tools - nothing here precludes it, and nothing here needs it
  yet.
