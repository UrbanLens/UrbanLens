# UrbanLens v0.6.0 — Server/Agent Architectural Split

Plan is deferred for now. May be reconsidered in the future.

Context
UrbanLens is currently a single Django/GeoDjango monolith (src/urbanlens, one first-party app urbanlens.dashboard, PostGIS, Celery, HTMX/Leaflet frontend). All user data — pins, notes, visits, photos, profiles — sits plaintext in one database, and media is served by nginx with no auth check. The E2EE design doc (docs/e2ee.md) explicitly names the unclosed gap: nothing protects users from a malicious or compromised server operator. ROADMAP UL-102 is the owner's own prior analysis of this exact split.

This release splits the app into two separately deployable services:

UrbanLens Server (central, project-operated): near-zero knowledge. Irreducible roles: coordinate→location matching (it must hold Location coordinates/boundaries), the shared community Wiki corpus, authentication/IdP, a minimal pin registry for access gating, and a blind relay for cross-agent communication. Threat model: a hacker or rogue admin with full access to the server must not be able to read user data.
UrbanLens Agent (holds real user data): profiles, pins, visits, photos, markup maps, trips, safety check-ins. The project runs a public multi-user agent (vast majority of users); users can self-host their own agent tied into the ecosystem, and agents are built to point at a configurable server. Desktop/mobile apps (next releases) will talk to their agent.
Binding decisions (made by the owner during planning)
Physical split this release — two services, two databases, migration for existing installs. Accepted long release.
Cross-user features: direct agent-to-agent is the ideal; server-as-blind-relay where direct isn't possible; same-agent-only unacceptable. Where an existing feature requires server knowledge, keep it — but only the minimum necessary.
Server identity: minimal account — credentials (password/SSO/2FA/passkeys), email for recovery + critical notifications. No profile data.
Server keeps a pin registry: (account, location) pairs only — no names/notes/dates/photos — preserving wiki gating, share provenance, fuzzed community counts.
Coordinates cannot be hidden from the server (matching requires them); no PSI/PIR crypto in v1.
The agent must never learn of locations the user hasn't pinned/been-exposed-to (no enumeration).
Handle: username doubles as the public community handle (as today).
Wiki media: server-hosted shared corpus (ClamAV re-scanned, minimal attribution).
Cross-agent trips v0.6.0 bar: invite + accept + activity sync via relay envelopes; real-time co-editing deferred.
Agent registration: open, rate-limited, revocable credentials.
Target architecture
Monorepo, uv workspace, three packages, two deployables:

src/
  urbanlens_core/     # shared library: contract models, validators, EncryptedTextField,
                      # plugin framework + hook bus, gateway/rate-limiter core,
                      # E2EE wire vocabulary, markdown rendering, abstract model bases
  urbanlens_server/   # NEW Django project, fresh migrations. Apps:
                      #   accounts (auth/AccountKdf/WebAuthn/TOTP/social_django)
                      #   atlas    (Location, location/wiki Boundary, GooglePlace, LocationCache, EpaFacility)
                      #   wiki     (Wiki + all Wiki* satellites, WikiComment, wiki media)
                      #   registry (PinRegistry, AgentRegistration)
                      #   relay    (RelayEnvelope, key directory, RelayConsent)
  urbanlens/          # EXISTING package becomes the Agent — keeps app label `dashboard`
                      # and existing migration history (no history surgery on installs)
The strangler seam (what keeps every phase shippable): the agent talks to the server exclusively through a ServerClient protocol (dashboard/services/server_client/) with LocalServerClient (in-process, current models) and RemoteServerClient (HTTP). UL_SERVER_MODE=embedded|remote; embedded is the default until Phase 5.

What runs where

Both sides keep PostGIS (agent still has Route LineString, PinMarkup geometry, pin/profile Boundary rows; plain-Postgres agent is a non-goal this release). Both sides run their own Valkey + Celery. dashboard/tasks.py (~95 tasks) splits: server gets location enrichment beat, wiki maintenance, relay expiry; agent keeps photo import, safety escalation (5-min beat), integration syncs, exports, both worker pools.
Web UI is served by the agent — all templates/HTMX/TS bundles stay. Wiki pages render on the agent from wiki JSON fetched via ServerClient (markdown utils in urbanlens_core keep output identical). Server has only a minimal account portal (login, 2FA, passkeys, email, delete).
Plugins gain a side attribute: server-billed shared-key integrations (Google/Azure/Yelp/OpenAI/NPS/OpenWeatherMap/REData…) run server-side behind the enrichment API with LocationCache; per-user OAuth (Immich/Flickr/Google Photos/Calendar/Ollama/SearXNG) and keyless public APIs run agent-side.
Agent media gets authenticated serving this release (nginx X-Accel-Redirect internal location) — fixes the existing unauthenticated location /media/ hole in config/nginx/django.conf.
Clients: browsers → agent (sessions, unchanged UX). Future desktop/mobile → agent /api/v1/ growing from dashboard/external_api/ + docs/external_app_api_plan.md. Agents → server /api/agent/v1/.
Data ownership map
Server: auth.User (credentials, email, username-as-handle), AccountKdf, WebAuthn/TOTP/social rows, E2EE key bundles (public parts + wrapped-private ciphertext), Location, Boundary (location/wiki-owned rows), GooglePlace, LocationCache, EpaFacility, Wiki + WikiAlias/Link/Owner/PropertySale/Edit/StatVote/AutoRemoval, Article(wiki), WikiComment, wiki-parented PinMarkup, wiki media, PinRegistry (NEW: account, location, kind∈{PINNED, EXPOSED}, source_account nullable), AgentRegistration (NEW), RelayEnvelope/RelayConsent (NEW), server ApiCallLog/ApiRateLimit/SiteSettings-half, account EmailLog.

Agent: Profile (all visibility fields/preferences), shadow auth.User (no password, keyed by server account UUID), Pin, PinVisit, PinAlias/Link/Owner/PropertySale/AutoRemoval, Review, Comment(pin), Article(pin), MarkupMap, PinMarkup(pin/map), Image, Route, Label (user rows; global rows become versioned fixtures + hourly GET /catalog/labels sync), PinShare, Trip family, SafetyCheckin family (fully agent-side incl. escalation emails — self-hosted agents need SMTP; server dead-man's-switch is a non-goal), Notification*, DM/GroupChat families, conversation/group wrapped keys, integration accounts, PinSuggestion, SearchHistory, SavedFilter, CustomField*, ApiKey (becomes client PAT), friendship, agent SiteSettings/ApiCallLog, LocationRef (NEW).

LocationRef (dashboard/models/location_ref/) is the linchpin: local cache row (server_uuid PK, lat, lng, point, name, address fields, slug, has_wiki, boundary_geojson, synced_at), populated only from pin prepare/commit responses, accepted exposures, and registry sync — never enumeration. Pin.location, Image.location, TripActivity.location, SafetyCheckin.destination_location, PinVisit paths repoint to it, preserving ORM joins, map rendering, and the localStorage pin cache without per-request server round trips.

Hard cases resolved: Wiki attribution FKs server accounts via username-handle (gated on PinRegistry — no new leakage). Boundary partitions on its existing owner discriminator. PinShare content is agent-side/E2EE; server-side provenance collapses into PinRegistry EXPOSED rows (share_provenance.py + community_counts.py port to server). Trips: organizer's agent is source of truth; cross-agent members sync via envelopes; accepting an activity at an unpinned location registers an EXPOSED row with the same consent flow as a share.

Agent↔Server API contract
Defined once as pydantic models in urbanlens_core/contract/ (imported by LocalServerClient, RemoteServerClient, and server views); OpenAPI emitted. URL major version + X-UL-Contract header; GET /.well-known/urbanlens-server advertises supported range + capabilities (the hook for server-switching later).

Auth, two layers (both reuse the hashed-bearer pattern from dashboard/external_api/authentication.py + services/api_keys.py, which moves to core):

Agent credential uls_agent_<prefix>_<secret> from manage.py register_agent (open, rate-limited, revocable) — sent as X-UL-Agent-Key.
Per-user OAuth2 bearer token from the auth flow below. Never wired into DRF defaults.
Endpoint families: pins prepare/commit (POST /pins/prepare {lat,lng} → point-matched candidates with boundary GeoJSON — preserves the multi-location ambiguity chooser; POST /pins/commit idempotent via client_nonce, writes PinRegistry PINNED, returns LocationRef payload); registry (delete, POST /exposures, full GET /registry sync); wiki CRUD + votes/edits/comments/attachments (gate = wiki_access.py ported against PinRegistry; unpinned ≡ nonexistent ≡ no-wiki, byte-identical 404 — oracle-free property preserved verbatim); community fuzzed counts; enrichment panels (server-billed, LocationCache-backed, entitlement-gated); relay (post/poll/ack envelopes, key directory, consents); account/OIDC.

Anti-enumeration: per-account human-scale quotas on prepare/commit (SiteSettings-tunable), per-agent aggregate ceilings scaled by active accounts, velocity checks via existing ApiRateLimit machinery, no bulk/nearest-N/list endpoint exists in the contract at all (enforced by contract tests). Documented honestly per UL-102: quotas raise cost; they are not cryptography.

Identity & auth
Server is the IdP: signup, derived-credential login (authKey/wrapKey unchanged), Google/Discord SSO, TOTP, passkeys, recovery all move to urbanlens_server.accounts (services/auth_backend.py, webauthn.py, two_factor.py, social_auth/ relocate). Agent becomes a relying party: django-oauth-toolkit (OAuth2+OIDC) on the server, consumed via social-core's generic OIDC backend (social_django already installed on agent). Flow: agent → auth-code+PKCE redirect → server login (2FA/passkey unchanged) → agent creates shadow auth.User keyed by account UUID → normal Valkey session; refresh tokens stored with EncryptedTextField.

E2EE improves: on a self-hosted agent, the agent ships e2ee-client.ts/e2ee-crypto.ts, closing the "server ships the JS" gap for message content. Cutover invalidates all sessions (release-notes item). Server-switching is design-doc-only this release (UL_SERVER_URL + well-known discovery keep it possible).

Cross-agent social (blind relay)
One primitive: sealed X25519/secretbox envelopes (existing E2EE vocabulary). Sender seals against recipient's public bundle from the key directory, posts to /relay/envelopes; server stores opaque blobs until acked or TTL (default 30 days for offline self-hosted agents). v1 delivery = agent Celery polling (public agent batches one poll); WS push over server Daphne is a fast-follow. Same-agent interactions short-circuit locally with identical semantics.

Pin shares: envelope {location_uuid, message, sender proof} → accept → POST /exposures → LocationRef + local PinShare (services/pin_sharing.py refactor).
DMs/groups: already E2EE; cross-agent = envelopes.
Friendships: handshake envelopes; accept registers a bare RelayConsent(a,b) spam-gate edge.
Trips: invite/accept/activity-sync op-log envelopes, organizer's agent authoritative.
urbanlens_core/relay/transport.py defines a Transport protocol; RelayTransport is the only v1 impl — future DirectTransport (agent-to-agent) slots in without touching feature code. No NAT traversal now.
Migration for existing installs
The big data stays put: the existing DB becomes the public-agent DB; the server DB is fresh and small.

manage.py export_server_data ETL (on 0.5.x schema): copies Location, location/wiki Boundaries, Wiki*, GooglePlace, LocationCache, EpaFacility, auth+AccountKdf+social/WebAuthn/TOTP rows, public E2EE bundles, wiki media; synthesizes PinRegistry from Pin(profile,location) + LocationExposure. Emits a count/checksum manifest.
Agent migrations: create + populate LocationRef (only locations this install's data references), repoint cross-boundary FKs, export-and-delete wiki-parented comments/markup, then a destructive drop of server-owned tables gated behind UL_CONFIRM_SPLIT=1, sequenced last and deferrable days after cutover.
Cutover runbook: maintenance window → full backup → ETL → manifest verify → agent migrations → deploy docker-compose.server.yml + docker-compose.agent.yml → smoke suite. Rollback = restore dump + pinned 0.5.x ghcr image; trivial until the drop migration.
Self-host bootstrap: docker compose -f docker-compose.agent.yml up → register_agent --server … → users log in via OIDC → LocationRef fills lazily. Separate .env per stack; pydantic settings split into server/agent halves (both UL_-prefixed).
Phasing (each phase leaves the codebase shippable)
Phase	Goal	Key modules	Verification
0	ADR + contract v0 + data-ownership table committed	docs/architecture/server-agent-split.md, urbanlens_core/contract/	review; contract imports in CI
1	uv workspace; extract urbanlens_core (validators, fields.py, plugins, gateway core, e2ee constants, markdown)	pyproject.toml, src/urbanlens_core/*	full existing suite green
2	ServerClient protocol + LocalServerClient; rewrite all call sites (pin_creation.py, map/wiki controllers, wiki_access/community_counts/share_provenance consumers, enrichment tasks)	dashboard/services/server_client/	hypothesis suite green; contract-corpus tests vs LocalServerClient
3	Stand up urbanlens_server (5 apps, fresh migrations); RemoteServerClient; UL_SERVER_MODE flag; server image + compose	src/urbanlens_server/*, remote.py, compose	corpus tests pass identically Local vs Remote; e2e compose boots
4	Auth split: django-oauth-toolkit, OIDC backend, shadow users, account portal	urbanlens_server/accounts/*	auth e2e (signup/SSO/2FA/passkey/recovery); e2ee interop tests
5	Data move: LocationRef + FK repoints; wiki UI → remote fetch; PinRegistry live; quotas; remote mode default	dashboard/models/location_ref/, migrations, urbanlens_server/registry/	knowledge-audit test; wiki 404-parity + scoping tests; quota tests
6	Relay + cross-agent: envelopes, key directory, consents, poller; shares/DMs/friendships/trip-invites	urbanlens_server/relay/*, urbanlens_core/relay/transport.py	two-agent e2e: share→accept→wiki-visible; DM round trip
7	Migration tooling + packaging: ETL + manifest, runbook, gated drop, two ghcr images, self-host guide	management commands, docs/migration-0.6.md, workflows	staged rehearsal on prod-copy dump; timed rollback drill
8	Hardening: authenticated media (X-Accel-Redirect), quota tuning, remove docs/prompts/ committed secrets and rotate them, threat-model doc update	config/nginx/django.conf, docs	security checklist; enumeration-attempt abuse test
Verification strategy
Hypothesis suite (~200 files) survives: agent-side tests keep passing against LocalServerClient (retained permanently as the test double); ported logic's tests move to urbanlens_server/tests/ largely verbatim; agent tests needing a server use urbanlens_core.contract.testing.FakeServer.
Contract equivalence corpus: property-based suite asserting FakeServer ≡ LocalServerClient ≡ real server (test client) — same-behavior-across-the-wire as a CI invariant.
Server-knowledge audit (headline test): test_knowledge_budget.py introspects every server model/field against a checked-in server_knowledge_allowlist.toml; any addition fails CI until explicitly justified. Companion lint: urbanlens_server may not import urbanlens.dashboard.
Privacy invariants: byte-identical wiki 404 parity, no-enumeration (no list endpoint in contract), LocationRef-only-from-consented-flows, quotas under simulated scanning.
E2E compose profile: server + two agents; signup → OIDC login → pin → wiki edit → cross-agent share → accept → wiki visible → DM round trip. Nightly CI.
Risks & non-goals
Risks: scope (mitigated by embedded-mode strangler — monolith works until Phase 5); migration data loss (manifest verify + rehearsal + destructive step last); forced re-login at cutover; remote-mode map latency (LocationRef caching, measure in Phase 3); relay polling latency (WS fast-follow); self-host support burden (honest docs: SMTP, backups, safety-feature availability); hostile agents (can only act as accounts that authenticate through them + quotas + revocation).

Non-goals v0.6.0: coordinate-hiding crypto (PSI/PIR), P2P/NAT traversal, server-switching UI, plain-Postgres agent, the desktop/mobile apps themselves, server-to-server federation, real-time cross-agent trip co-editing, E2EE media, safety dead-man's-switch on server, multi-server accounts.

Deferred minor decisions (defaults chosen): relay TTL 30 days; global labels as fixtures + server catalog endpoint; subscription entitlements enforced server-side at the enrichment API and mirrored read-only to agents for UI gating; public domain layout (server.urbanlens.com vs paths) decided at Phase 7 deployment.

Critical existing files
src/urbanlens/dashboard/services/pin_creation.py — single pin-creation path → prepare/commit seam
src/urbanlens/dashboard/services/wiki_access.py — oracle-free gate → server-side against PinRegistry
src/urbanlens/dashboard/models/location/queryset.py — matching logic the server inherits
src/urbanlens/dashboard/external_api/authentication.py + services/api_keys.py — hashed-bearer auth pattern reused for agent credentials and client PATs
src/urbanlens/dashboard/services/share_provenance.py, community_counts.py — port to server against PinRegistry
docs/e2ee.md — E2EE vocabulary + threat model this work extends
docker-compose.yml, src/urbanlens/UrbanLens/settings/app.py — templates