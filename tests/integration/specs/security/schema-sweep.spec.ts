/**
 * Every published endpoint that addresses an object by id, probed with another account's object.
 * The endpoint list comes from the live `schema/`, so a new endpoint is covered the day it ships: a
 * path parameter this file has no rule for fails the run and names itself.
 *
 * Per endpoint, as the stranger (`secondary`): a read must answer exactly as it does for an object
 * that never existed, and a write must be refused. The owner reads every endpoint before (control:
 * 2xx) and after (nothing the stranger sent landed, nothing the owner built is gone).
 */

import type { APIRequestContext } from "@playwright/test";

import type { ApiClient, CreatedPin } from "../../lib/api-client.js";
import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import {
    createChild,
    createCustomField,
    createLabel,
    createList,
    createSavedFilter,
    createTrip,
    idOf,
    isUnavailable,
    logVisit,
    middayUtc,
    openCheckin,
    randomMarker,
    rowsOf,
    uploadPhoto,
} from "../../lib/object-factories.js";
import { MISSING_SLUG, MISSING_UUID } from "../../lib/security.js";
import { locationSlugOf, placelessCoordinates, waitForWiki, writeArticle } from "../../lib/wiki.js";

const API_PREFIX = "/dashboard/api/external/v1/";
const E2EE_PREFIX = "/dashboard/e2ee/";

type Method = "get" | "post" | "put" | "patch" | "delete";
const METHODS: readonly Method[] = ["get", "post", "put", "patch", "delete"];

interface SchemaObject {
    $ref?: string;
    type?: string;
    format?: string;
    enum?: unknown[];
    readOnly?: boolean;
    properties?: Record<string, SchemaObject>;
    required?: string[];
    items?: SchemaObject;
    oneOf?: SchemaObject[];
    anyOf?: SchemaObject[];
    allOf?: SchemaObject[];
    maxLength?: number;
    minimum?: number;
    maximum?: number;
    minItems?: number;
}

interface Parameter {
    name: string;
    in: string;
    schema?: SchemaObject;
}

interface Operation {
    parameters?: Parameter[];
    requestBody?: { content?: Record<string, { schema?: SchemaObject }> };
}

interface OpenApiDocument {
    paths: Record<string, Record<string, unknown>>;
    components?: { schemas?: Record<string, SchemaObject> };
}

interface Endpoint {
    /** As published, e.g. `/dashboard/api/external/v1/pins/{pin_slug}/notes/`. */
    template: string;
    /** Relative to its mount; e2ee paths keep an `e2ee/` head so they classify like the rest. */
    rel: string;
    mount: "api" | "e2ee";
    method: Method;
    op: Operation;
    params: Array<{ name: string; schema: SchemaObject }>;
}

// -- Families ----------------------------------------------------------------
//
// One test per family keeps a failure's blast radius (and its setup cost) local. A new top-level
// segment fails the meta test until it is placed here.

const FAMILY_BY_SEGMENT: Record<string, string> = {
    pins: "pins",
    photos: "photos",
    lists: "lists",
    labels: "labels, saved filters and custom fields",
    "saved-filters": "labels, saved filters and custom fields",
    "custom-fields": "labels, saved filters and custom fields",
    trips: "trips",
    safety: "safety check-ins",
    wikis: "wikis",
    profiles: "profiles and friends",
    friends: "profiles and friends",
    messages: "messaging and key exchange",
    e2ee: "messaging and key exchange",
    notifications: "notifications, undo, devices, suggestions, shares, games and the assistant",
    undo: "notifications, undo, devices, suggestions, shares, games and the assistant",
    "push-devices": "notifications, undo, devices, suggestions, shares, games and the assistant",
    suggestions: "notifications, undo, devices, suggestions, shares, games and the assistant",
    "pin-shares": "notifications, undo, devices, suggestions, shares, games and the assistant",
    games: "notifications, undo, devices, suggestions, shares, games and the assistant",
    assistant: "notifications, undo, devices, suggestions, shares, games and the assistant",
};

const FAMILIES = [...new Set(Object.values(FAMILY_BY_SEGMENT))];

function familyOf(rel: string): string | null {
    return FAMILY_BY_SEGMENT[rel.split("/")[0] ?? ""] ?? null;
}

// -- Parameter rules ---------------------------------------------------------

/** Objects the sweep builds as the owner. Every one is created for the sweep and deleted after it. */
type OwnedKey =
    | "pin"
    | "pinNote"
    | "pinAlias"
    | "pinLink"
    | "pinVisit"
    | "pinComment"
    | "pinRevision"
    | "list"
    | "trip"
    | "tripActivity"
    | "tripComment"
    | "tripMember"
    | "label"
    | "savedFilter"
    | "customField"
    | "photo"
    | "checkin"
    | "checkinUuid"
    | "wiki"
    | "wikiAlias"
    | "wikiLink"
    | "wikiComment"
    | "wikiRevision"
    | "wikiEdit"
    | "undoEntry"
    | "pushDevice";

/** Parameters that pick *what* on an object rather than *whose*; the missing-object control keeps them. */
type SelectorKey = "emoji" | "statField" | "panelKey" | "suggestionAction";

const OWNED_BY_PARAM: Record<string, OwnedKey | Partial<Record<string, OwnedKey>>> = {
    pin_slug: "pin",
    list_slug: "list",
    trip_slug: "trip",
    label_uuid: "label",
    filter_uuid: "savedFilter",
    field_id: "customField",
    image_uuid: "photo",
    checkin_slug: "checkin",
    checkin_uuid: "checkinUuid",
    location_slug: "wiki",
    undo_uuid: "undoEntry",
    device_uuid: "pushDevice",
    // Child ids are keyed by the family they hang off: a comment id under pins/ is not one under wikis/.
    comment_id: { pins: "pinComment", wikis: "wikiComment", trips: "tripComment" },
    note_id: { pins: "pinNote" },
    alias_id: { pins: "pinAlias", wikis: "wikiAlias" },
    link_id: { pins: "pinLink", wikis: "wikiLink" },
    visit_id: { pins: "pinVisit" },
    activity_id: { trips: "tripActivity" },
    revision_id: { pins: "pinRevision", wikis: "wikiRevision" },
    edit_id: { wikis: "wikiEdit" },
    member_slug: { trips: "tripMember" },
};

const SELECTOR_BY_PARAM: Record<string, SelectorKey> = {
    emoji: "emoji",
    field: "statField",
    panel_key: "panelKey",
    action: "suggestionAction",
};

/**
 * Parameters the suite cannot mint a real owner-side instance of. They are still probed, with a
 * value that exists for nobody, which proves only "no crash, no 2xx"; the reason names the code that
 * scopes the lookup to the caller instead.
 */
const PLACEHOLDERS: Record<string, string> = {
    suggestion_id:
        "suggestions are produced by background photo/import analysis, not by any API call; both action views filter by the caller (external_api/views.py:1571-1575, 1655)",
    share_id:
        "a pin share needs an accepted friendship and the web send flow (no external route); the respond view looks it up with to_profile=caller (views_pin_shares.py:57-61). specs/security/pin-share.spec.ts owns it",
    turn_id: "an assistant turn bills a model provider; poll and confirm compare the cached turn's profile_id with the caller (views_assistant.py:183, 230)",
    n: "an assistant proposal index, only meaningful under a real turn (see turn_id)",
    session_id:
        "a SpotGuessr session is alpha-gated and bills Street View; every session view resolves through GameSessionParticipant filtered by the caller (views_games.py:201, 727)",
    round_id: "only meaningful under a real session (see session_id)",
    partner_id: "needs a second account to accept a partner invite; the delete filters by the owner-scoped check-in (external_api/views.py:3344)",
    image_id: "the check-in photo delete filters by the owner-scoped check-in and profile=caller (external_api/views.py:3407)",
    map_uuid: "attaching needs a markup map, which the external API cannot create; the delete filters through the owner-scoped check-in (external_api/views.py:3469)",
    notification_uuid:
        "marking read answers 204 whether or not a row matched, by design (external_api/views.py:4206-4213), and whether a stranger's call flipped the owner's row cannot be observed without racing notifications.spec.ts's read-all",
};

/** Parameters whose endpoints are not object reads or writes at all. */
const EXEMPT_PARAMS: Record<string, string> = {
    profile_uuid:
        "friends/{uuid}/... are relationship verbs (request, accept, block, mute) addressed to another person by design; sending them would rewrite the social graph social.spec.ts and the sharing specs depend on. specs/api/social.spec.ts covers them",
};

/** Endpoints skipped even though their parameters have rules. */
const EXEMPT_ENDPOINTS: ReadonlyArray<{ pattern: RegExp; methods?: Method[]; why: string }> = [
    {
        pattern: /^profiles\/\{profile_slug\}\/(annotations|nickname|trust|notes)\//,
        why: "the caller's own private annotations about another profile (ProfileNotesView, views_social.py); nothing of the subject's is read or written",
    },
    {
        pattern: /^profiles\/\{profile_slug\}\/social-links\/$/,
        methods: ["get"],
        why: "public social links, governed by the subject's profile_visibility like the profile itself",
    },
];

/**
 * Refused for every API key before any lookup: `messages:*` scopes are OAuth2-only
 * (external_api/permissions.py:32, 59), and every messaging and key-exchange view requires one.
 */
const REFUSED_FOR_KEYS = ["messages/", "e2ee/"];

/** Owner-side controls that differ from "the owner GETs the same URL and gets 2xx". */
const OWNER_READS: Record<string, { via?: string; statuses?: number[]; why: string }> = {
    "safety/partner-checkins/{checkin_uuid}/": {
        via: "safety/checkins/{checkin_uuid}/",
        why: "the partner surface excludes the owner by design (views_safety_chat.py:161-170); the owner reads the same check-in by uuid",
    },
    "pins/{pin_slug}/panels/{panel_key}/": {
        statuses: [200, 202, 204, 404],
        why: "404 when the deployment exposes no panel for a new pin",
    },
};

/** Non-refusals that are correct for a stranger. */
const UNIFORM_ANSWERS: Record<string, number[]> = {
    "notifications/{notification_uuid}/": [204],
};

/**
 * Reads whose state moves on its own while a new object is enriched in the background (wiki fields,
 * panel readiness, auto-applied pin labels). Their protection is the marker check and the write status.
 */
const SELF_CHANGING: readonly RegExp[] = [/^wikis\//, /\/panels\//, /^pins\/\{pin_slug\}\/$/];

/** Response keys whose value only changes when somebody acts on the object. Timestamps are left out on purpose. */
const STATE_KEYS = new Set([
    "status",
    "rsvp",
    "user_vote",
    "your_vote",
    "my_vote",
    "vote_up",
    "vote_down",
    "score",
    "is_organizer",
    "membership_status",
    "is_current",
    "is_resolved",
    "auto_sync",
    "linked",
    "is_customized",
    "reactions",
    "rating",
    "value",
]);

type Rule =
    | { kind: "owned"; key: OwnedKey }
    | { kind: "selector"; key: SelectorKey }
    | { kind: "placeholder"; why: string }
    | { kind: "profile" }
    | { kind: "exempt"; why: string };

function ruleFor(param: string, rel: string): Rule | null {
    if (param in EXEMPT_PARAMS) {
        return { kind: "exempt", why: EXEMPT_PARAMS[param] ?? "" };
    }
    if (param in PLACEHOLDERS) {
        return { kind: "placeholder", why: PLACEHOLDERS[param] ?? "" };
    }
    const selector = SELECTOR_BY_PARAM[param];
    if (selector) {
        return { kind: "selector", key: selector };
    }
    if (param === "profile_slug" && rel.startsWith("profiles/")) {
        return { kind: "profile" };
    }
    const owned = OWNED_BY_PARAM[param];
    if (typeof owned === "string") {
        return { kind: "owned", key: owned };
    }
    const nested = owned?.[rel.split("/")[0] ?? ""];
    return nested ? { kind: "owned", key: nested } : null;
}

type Plan =
    | { kind: "skip"; why: string }
    | { kind: "unruled"; params: string[] }
    | { kind: "refused" }
    | { kind: "profile" }
    | { kind: "owned" }
    | { kind: "unowned" };

function classify(endpoint: Endpoint): Plan {
    if (REFUSED_FOR_KEYS.some((prefix) => endpoint.rel.startsWith(prefix))) {
        return { kind: "refused" };
    }
    const exempt = EXEMPT_ENDPOINTS.find((entry) => entry.pattern.test(endpoint.rel) && (!entry.methods || entry.methods.includes(endpoint.method)));
    if (exempt) {
        return { kind: "skip", why: exempt.why };
    }
    const rules = endpoint.params.map((param) => ({ name: param.name, rule: ruleFor(param.name, endpoint.rel) }));
    const unruled = rules.filter((entry) => entry.rule === null).map((entry) => entry.name);
    if (unruled.length > 0) {
        return { kind: "unruled", params: unruled };
    }
    const exemptRule = rules.find((entry) => entry.rule?.kind === "exempt");
    if (exemptRule?.rule?.kind === "exempt") {
        return { kind: "skip", why: exemptRule.rule.why };
    }
    if (rules.some((entry) => entry.rule?.kind === "profile")) {
        // The profile is the owner's real one, so nothing destructive is sent at it.
        return endpoint.method === "delete" ? { kind: "skip", why: "DELETE is only sent at objects the sweep created; this profile is the owner's real one" } : { kind: "profile" };
    }
    return rules.some((entry) => entry.rule?.kind === "owned") ? { kind: "owned" } : { kind: "unowned" };
}

// -- Schema --------------------------------------------------------------------

function endpointsOf(doc: OpenApiDocument): Endpoint[] {
    const endpoints: Endpoint[] = [];
    for (const [template, item] of Object.entries(doc.paths)) {
        const mount = template.startsWith(API_PREFIX) ? "api" : template.startsWith(E2EE_PREFIX) ? "e2ee" : null;
        if (mount === null) {
            continue;
        }
        const rel = mount === "api" ? template.slice(API_PREFIX.length) : `e2ee/${template.slice(E2EE_PREFIX.length)}`;
        const shared = (item.parameters as Parameter[] | undefined) ?? [];
        for (const method of METHODS) {
            const op = item[method] as Operation | undefined;
            if (!op) {
                continue;
            }
            const declared = [...shared, ...(op.parameters ?? [])].filter((parameter) => parameter.in === "path");
            const params = [...template.matchAll(/\{([^}]+)\}/g)].map((match) => {
                const name = match[1] ?? "";
                return { name, schema: declared.find((parameter) => parameter.name === name)?.schema ?? { type: "string" } };
            });
            endpoints.push({ template, rel, mount, method, op, params });
        }
    }
    return endpoints;
}

async function loadSchema(anonymousApi: ApiClient): Promise<OpenApiDocument> {
    return anonymousApi.json<OpenApiDocument>("get", "schema/", { format: "json" });
}

function missingValueFor(schema: SchemaObject): string {
    if (schema.type === "integer") {
        return "2147483646";
    }
    return schema.format === "uuid" ? MISSING_UUID : MISSING_SLUG;
}

function fill(rel: string, values: Record<string, string>): string {
    return rel.replace(/\{([^}]+)\}/g, (_whole, name: string) => encodeURIComponent(values[name] ?? ""));
}

const TEXT_PROPERTY = /^(name|title|text|body|content|description|notes|caption|plan_details|contact_message|message|edit_summary|bio)$/;

/**
 * A request body the operation's own schema would accept, with every free-text field set to `marker`.
 *
 * Valid rather than empty so a view that validates before it looks the object up still reaches the
 * lookup - an empty body answers 400 without ever asking whose object it is.
 */
function sampleBody(doc: OpenApiDocument, op: Operation, marker: string): unknown {
    const content = op.requestBody?.content;
    if (!content) {
        return undefined;
    }
    const schema = (content["application/json"] ?? Object.values(content)[0])?.schema;
    return schema ? sample(doc, schema, marker, "", 0) : undefined;
}

function resolveRef(doc: OpenApiDocument, schema: SchemaObject): SchemaObject {
    let current = schema;
    for (let hops = 0; current.$ref && hops < 10; hops += 1) {
        current = doc.components?.schemas?.[current.$ref.split("/").pop() ?? ""] ?? {};
    }
    return current;
}

function sample(doc: OpenApiDocument, raw: SchemaObject, marker: string, name: string, depth: number): unknown {
    const schema = resolveRef(doc, raw);
    const variant = schema.allOf?.[0] ?? schema.oneOf?.find((option) => !resolveRef(doc, option).enum?.includes(null)) ?? schema.anyOf?.[0];
    if (variant) {
        return sample(doc, variant, marker, name, depth);
    }
    if (schema.enum) {
        return schema.enum.find((value) => value !== null) ?? null;
    }
    if (schema.type === "object" || schema.properties) {
        if (depth > 3) {
            return {};
        }
        const required = new Set(schema.required ?? []);
        const body: Record<string, unknown> = {};
        for (const [key, property] of Object.entries(schema.properties ?? {})) {
            const resolved = resolveRef(doc, property);
            // A partial update with no required fields still needs *something* to validate; flags rarely fail.
            const flag = required.size === 0 && resolved.type === "boolean";
            if (resolved.readOnly || !(required.has(key) || TEXT_PROPERTY.test(key) || flag)) {
                continue;
            }
            body[key] = sample(doc, property, marker, key, depth + 1);
        }
        return body;
    }
    switch (schema.type) {
        case "array":
            return (schema.minItems ?? 0) > 0 && schema.items ? [sample(doc, schema.items, marker, name, depth + 1)] : [];
        case "boolean":
            return true;
        case "integer":
            return Math.min(schema.maximum ?? Number.MAX_SAFE_INTEGER, Math.max(schema.minimum ?? 1, 1));
        case "number":
            if (/lat/.test(name)) {
                return 42.65;
            }
            if (/lng|lon/.test(name)) {
                return -73.75;
            }
            return Math.min(schema.maximum ?? Number.MAX_SAFE_INTEGER, Math.max(schema.minimum ?? 1, 1));
        case "string":
            switch (schema.format) {
                case "date-time":
                    return middayUtc(1);
                case "date":
                    return middayUtc(1).slice(0, 10);
                case "uuid":
                    return MISSING_UUID;
                case "uri":
                    return `https://example.invalid/${marker}`;
                case "email":
                    return `${marker}@example.invalid`;
                case "decimal":
                    return "1.0";
                default:
                    return marker.slice(0, schema.maxLength ?? marker.length);
            }
        default:
            return null;
    }
}

// -- Transport -------------------------------------------------------------------

interface Answer {
    status: number;
    body: string;
}

const THROTTLE_PATIENCE = 3;

/** Sends one request as `client`, riding out a 429 the client's own retries could not. */
async function send(request: APIRequestContext, client: ApiClient, endpoint: Pick<Endpoint, "mount" | "method">, path: string, body?: unknown): Promise<Answer> {
    const once = async () => {
        if (endpoint.mount === "e2ee") {
            return request.fetch(`${E2EE_PREFIX}${path.replace(/^e2ee\//, "")}`, {
                method: endpoint.method.toUpperCase(),
                headers: client.apiKey ? { Authorization: `Bearer ${client.apiKey}`, Accept: "application/json" } : {},
                data: endpoint.method === "get" || endpoint.method === "delete" ? undefined : (body ?? {}),
            });
        }
        switch (endpoint.method) {
            case "get":
                return client.get(path);
            case "delete":
                return client.delete(path);
            default:
                return client[endpoint.method](path, body ?? {});
        }
    };
    let response = await once();
    for (let attempt = 0; attempt < THROTTLE_PATIENCE && response.status() === 429; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 30_000));
        response = await once();
    }
    return { status: response.status(), body: await response.text() };
}

async function inBatches<T>(items: readonly T[], width: number, run: (item: T) => Promise<void>): Promise<void> {
    for (let start = 0; start < items.length; start += width) {
        await Promise.all(items.slice(start, start + width).map(run));
    }
}

function stateOf(body: string): string {
    let parsed: unknown;
    try {
        parsed = JSON.parse(body);
    } catch {
        return "";
    }
    const found: string[] = [];
    const walk = (value: unknown, path: string) => {
        if (Array.isArray(value)) {
            value.forEach((entry, index) => walk(entry, `${path}[${index}]`));
        } else if (value && typeof value === "object") {
            for (const [key, child] of Object.entries(value)) {
                if (STATE_KEYS.has(key)) {
                    found.push(`${path}.${key}=${JSON.stringify(child)}`);
                } else {
                    walk(child, `${path}.${key}`);
                }
            }
        }
    };
    walk(parsed, "");
    return found.sort().join("\n");
}

// -- Owner-side objects ----------------------------------------------------------

class UnavailableError extends Error {}

/** Builds each owner-side object at most once per test, and remembers how to prove it still exists. */
class Registry {
    private readonly built = new Map<string, Promise<string>>();
    private readonly pins = new Map<string, Promise<CreatedPin>>();
    readonly checks: Array<{ what: string; run: () => Promise<string | null> }> = [];

    constructor(
        readonly api: ApiClient,
        readonly request: APIRequestContext,
        readonly marker: string,
    ) {}

    private once(key: string, build: () => Promise<string>): Promise<string> {
        let pending = this.built.get(key);
        if (!pending) {
            pending = build();
            this.built.set(key, pending);
        }
        return pending;
    }

    private pinFor(key: "sweep" | "wiki", build: () => Promise<CreatedPin>): Promise<CreatedPin> {
        let pending = this.pins.get(key);
        if (!pending) {
            pending = build();
            this.pins.set(key, pending);
        }
        return pending;
    }

    private expectListed(what: string, path: string, id: string): void {
        this.checks.push({
            what,
            run: async () => {
                const response = await this.api.get(path, { page_size: 100 });
                const body = await response.text();
                if (response.status() !== 200) {
                    return `${what}: the owner's GET ${path} answered ${response.status()} after the stranger's writes`;
                }
                return body.includes(`"${id}"`) || body.includes(`:${id},`) || body.includes(`:${id}}`) ? null : `${what} ${id} is no longer in the owner's ${path}`;
            },
        });
    }

    private expectReadable(what: string, path: string): void {
        this.checks.push({
            what,
            run: async () => {
                const response = await this.api.get(path);
                return response.status() === 200 ? null : `${what}: the owner's GET ${path} answered ${response.status()} after the stranger's writes`;
            },
        });
    }

    sweepPin(): Promise<CreatedPin> {
        return this.pinFor("sweep", async () => {
            const pin = await this.api.createPin({ name: `${resourceName("sweep pin")} ${this.marker}` });
            this.expectReadable("the sweep pin", `pins/${pin.slug}/`);
            return pin;
        });
    }

    private wikiPin(): Promise<CreatedPin> {
        // Placeless, so the wiki is reached by the exact-location rule alone and nothing else can grant it.
        return this.pinFor("wiki", () => this.api.createPin({ name: `${resourceName("sweep wiki pin")} ${this.marker}`, ...placelessCoordinates() }));
    }

    async owned(key: OwnedKey): Promise<string> {
        switch (key) {
            case "pin":
                return (await this.sweepPin()).slug;
            case "pinNote":
                return this.pinChild("note", "notes", { text: resourceName("sweep note") });
            case "pinAlias":
                return this.pinChild("alias", "aliases", { name: resourceName(`sweep alias ${crypto.randomUUID().slice(0, 6)}`) });
            case "pinLink":
                return this.pinChild("link", "links", { url: "https://example.invalid/urbanlens-sweep", name: resourceName("sweep link") });
            case "pinComment":
                return this.pinChild("comment", "comments", { text: resourceName("sweep comment") });
            case "pinVisit":
                return this.once(key, async () => {
                    const pin = await this.sweepPin();
                    const id = await logVisit(this.api, pin.slug, middayUtc(1), resourceName("sweep visit"));
                    this.expectListed("the sweep pin's visit", `pins/${pin.slug}/visits/`, id);
                    return id;
                });
            case "pinRevision":
                return this.once(key, async () => {
                    const pin = await this.sweepPin();
                    await this.api.json("put", `pins/${pin.slug}/article/`, { content: resourceName("sweep article"), base_revision_id: null, edit_summary: "sweep" });
                    const id = idOf(rowsOf(await this.api.json("get", `pins/${pin.slug}/article/revisions/`))[0]);
                    this.expectListed("the sweep pin's article revision", `pins/${pin.slug}/article/revisions/`, id);
                    return id;
                });
            case "list":
                return this.once(key, async () => {
                    const pin = await this.sweepPin();
                    const list = await createList(this.api, `${resourceName("sweep list")} ${this.marker}`);
                    await this.api.json("post", `lists/${list.slug}/items/`, { pin_uuids: [pin.uuid] });
                    this.expectReadable("the sweep list", `lists/${list.slug}/`);
                    this.expectListed("the sweep list's pin", `lists/${list.slug}/items/`, pin.uuid);
                    return list.slug;
                });
            case "trip":
                return this.once(key, async () => {
                    const trip = await createTrip(this.api, `${resourceName("sweep trip")} ${this.marker}`);
                    this.expectReadable("the sweep trip", `trips/${trip.slug}/`);
                    return trip.slug;
                });
            case "tripActivity":
                return this.tripChild("activity", "activities", { title: resourceName("sweep activity") });
            case "tripComment":
                return this.tripChild("comment", "comments", { text: resourceName("sweep trip comment") });
            case "tripMember":
                return this.once(key, async () => {
                    const trip = await this.owned("trip");
                    const members = rowsOf<{ profile?: { slug?: string; uuid?: string } }>(await this.api.json("get", `trips/${trip}/members/`));
                    const slug = members[0]?.profile?.slug ?? members[0]?.profile?.uuid ?? (await this.api.json<{ slug: string }>("get", "whoami/")).slug;
                    this.expectListed("the sweep trip's owner membership", `trips/${trip}/members/`, slug);
                    return slug;
                });
            case "label":
                return this.once(key, async () => {
                    const label = await createLabel(this.api, `${resourceName("sweep label")} ${this.marker}`);
                    this.expectReadable("the sweep label", `labels/${label.uuid}/`);
                    return label.uuid;
                });
            case "savedFilter":
                return this.once(key, async () => {
                    const filter = await createSavedFilter(this.api, `${resourceName("sweep filter")} ${this.marker}`);
                    this.expectReadable("the sweep saved filter", `saved-filters/${filter.uuid}/`);
                    return filter.uuid;
                });
            case "customField":
                return this.once(key, async () => {
                    // A photo field, so the same id also addresses photos/{uuid}/custom-fields/{field_id}/.
                    const field = await createCustomField(this.api, "photo", `${resourceName("sweep field")} ${crypto.randomUUID().slice(0, 6)}`);
                    this.expectReadable("the sweep custom field", `custom-fields/${field.id}/`);
                    return field.id;
                });
            case "photo":
                return this.once(key, async () => {
                    const pin = await this.sweepPin();
                    const photo = await uploadPhoto(this.request, this.api, pin.slug, `${resourceName("sweep photo")} ${this.marker}`);
                    if (isUnavailable(photo)) {
                        throw new UnavailableError(photo.unavailable);
                    }
                    const field = await this.owned("customField");
                    await this.api.json("put", `photos/${photo.uuid}/custom-fields/${field}/`, { value: resourceName("sweep value") });
                    this.expectReadable("the sweep photo", `photos/${photo.uuid}/`);
                    return photo.uuid;
                });
            case "checkin":
                return (await this.checkin()).slug;
            case "checkinUuid":
                return (await this.checkin()).uuid;
            case "wiki":
                return this.once(key, async () => {
                    const pin = await this.wikiPin();
                    const slug = await locationSlugOf(this.api, pin.slug);
                    await waitForWiki(this.api, slug);
                    this.expectReadable("the sweep wiki", `wikis/${slug}/`);
                    return slug;
                });
            case "wikiAlias":
                return this.wikiChild("alias", "aliases", { name: resourceName(`sweep wiki alias ${crypto.randomUUID().slice(0, 6)}`) });
            case "wikiLink":
                return this.wikiChild("link", "links", { url: "https://example.invalid/urbanlens-sweep-wiki", name: resourceName("sweep wiki link") });
            case "wikiComment":
                return this.wikiChild("comment", "comments", { text: resourceName("sweep wiki comment") });
            case "wikiRevision":
                return this.once(key, async () => {
                    const wiki = await this.owned("wiki");
                    const saved = await writeArticle(this.api, wiki, resourceName("sweep wiki article"), "sweep");
                    if (!saved.ok()) {
                        throw new Error(`writing the sweep wiki's article answered ${saved.status()}: ${(await saved.text()).slice(0, 200)}`);
                    }
                    const id = idOf(rowsOf(await this.api.json("get", `wikis/${wiki}/article/revisions/`))[0]);
                    this.expectListed("the sweep wiki's article revision", `wikis/${wiki}/article/revisions/`, id);
                    return id;
                });
            case "wikiEdit":
                return this.once(key, async () => {
                    const wiki = await this.owned("wiki");
                    // History records field edits; aliases and article revisions have their own trails.
                    const patched = await this.api.patch(`wikis/${wiki}/`, { description: `${resourceName("sweep wiki description")} ${this.marker}` });
                    if (!patched.ok()) {
                        throw new Error(`editing the sweep wiki's description answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`);
                    }
                    const edits = rowsOf(await this.api.json("get", `wikis/${wiki}/history/`));
                    if (edits.length === 0) {
                        throw new Error(`wikis/${wiki}/history/ lists nothing after a field edit, so there is no edit id to probe`);
                    }
                    return idOf(edits[0]);
                });
            case "undoEntry":
                return this.once(key, async () => {
                    const name = `${resourceName("sweep undo")} ${this.marker}`;
                    const pin = await this.api.createPin({ name });
                    const removed = await this.api.delete(`pins/${pin.slug}/`);
                    expect(removed.ok(), `deleting the undo sweep pin answered ${removed.status()}`).toBeTruthy();
                    const feed = await this.api.json<{ entries: Array<{ uuid: string; object_repr?: string }> }>("get", "undo/");
                    const entry = feed.entries.find((candidate) => candidate.object_repr?.includes(name));
                    if (!entry) {
                        throw new Error("the owner's undo feed has no entry for the pin the sweep just deleted");
                    }
                    this.checks.push({
                        what: "the sweep undo entry",
                        run: async () => ((await (await this.api.get("undo/")).text()).includes(entry.uuid) ? null : `the owner's undo entry ${entry.uuid} is gone after the stranger's restore attempt`),
                    });
                    return entry.uuid;
                });
            case "pushDevice":
                return this.once(key, async () => {
                    // FCM tokens are stored unvalidated, so no reachable endpoint is needed. Revocation is
                    // unobservable from the owner's side (DELETE answers 204 on a revoked row too), so the
                    // stranger's 404 is the whole evidence here.
                    const device = await this.api.json<{ uuid: string }>("post", "push-devices/", {
                        transport: "fcm",
                        address: `e2e-sweep-${crypto.randomUUID()}`,
                        name: resourceName("sweep device"),
                    });
                    this.api.track("push-device", device.uuid, () => this.api.delete(`push-devices/${device.uuid}/`));
                    return device.uuid;
                });
        }
    }

    async selector(key: SelectorKey): Promise<string> {
        switch (key) {
            case "emoji":
                return "\u{1F44D}";
            case "statField":
                return "danger";
            case "suggestionAction":
                return "accept";
            case "panelKey":
                return this.once(key, async () => {
                    const pin = await this.sweepPin();
                    const panels = rowsOf<{ key?: string }>(await this.api.json("get", `pins/${pin.slug}/panels/`));
                    return panels[0]?.key ?? "no-panel-exposed";
                });
        }
    }

    /** Owner-side setup some GET controls need before they can answer 2xx at all. */
    async precondition(rel: string): Promise<void> {
        if (rel === "pins/{pin_slug}/review/") {
            await this.once("pinReview", async () => {
                const pin = await this.sweepPin();
                await this.api.json("put", `pins/${pin.slug}/review/`, { rating: 3 });
                return "done";
            });
        } else if (rel.startsWith("pins/{pin_slug}/article/")) {
            await this.owned("pinRevision");
        } else if (rel.startsWith("wikis/{location_slug}/article/")) {
            await this.owned("wikiRevision");
        }
    }

    private checkin(): Promise<{ slug: string; uuid: string }> {
        return this.once("checkin", async () => {
            const checkin = await openCheckin(this.api, `${resourceName("sweep check-in")} ${this.marker}`);
            if (isUnavailable(checkin)) {
                throw new UnavailableError(checkin.unavailable);
            }
            this.expectReadable("the sweep check-in", `safety/checkins/${checkin.slug}/`);
            return JSON.stringify(checkin);
        }).then((raw) => JSON.parse(raw) as { slug: string; uuid: string });
    }

    private pinChild(what: string, collection: string, body: unknown): Promise<string> {
        return this.once(`pin:${collection}`, async () => {
            const pin = await this.sweepPin();
            const id = await createChild(this.api, `pins/${pin.slug}/${collection}/`, body);
            this.expectListed(`the sweep pin's ${what}`, `pins/${pin.slug}/${collection}/`, id);
            return id;
        });
    }

    private tripChild(what: string, collection: string, body: unknown): Promise<string> {
        return this.once(`trip:${collection}`, async () => {
            const trip = await this.owned("trip");
            const id = await createChild(this.api, `trips/${trip}/${collection}/`, body);
            this.expectListed(`the sweep trip's ${what}`, `trips/${trip}/${collection}/`, id);
            return id;
        });
    }

    private wikiChild(what: string, collection: string, body: unknown): Promise<string> {
        return this.once(`wiki:${collection}`, async () => {
            const wiki = await this.owned("wiki");
            const id = await createChild(this.api, `wikis/${wiki}/${collection}/`, body);
            this.expectListed(`the sweep wiki's ${what}`, `wikis/${wiki}/${collection}/`, id);
            return id;
        });
    }
}

// -- The sweep -------------------------------------------------------------------

interface Probe {
    endpoint: Endpoint;
    plan: Plan;
    /** URL path as the stranger sends it, with the owner's real ids. */
    path: string;
    /** The same path with every owner-side id swapped for one that exists for nobody. */
    missingPath: string;
    /** `[real, stand-in]` pairs, to normalise an echoed id before comparing bodies. */
    substitutions: Array<[string, string]>;
    /** Whether any parameter is a stand-in, which makes an owner-side control meaningless. */
    hasPlaceholder: boolean;
}

interface SweepReport {
    probed: string[];
    skipped: string[];
    unexercised: string[];
    problems: string[];
}

async function resolveProbe(registry: Registry, endpoint: Endpoint, plan: Plan, ownerSlug: string): Promise<Probe> {
    const values: Record<string, string> = {};
    const missing: Record<string, string> = {};
    const substitutions: Array<[string, string]> = [];
    let hasPlaceholder = false;
    await registry.precondition(endpoint.rel);
    for (const param of endpoint.params) {
        const standIn = missingValueFor(param.schema);
        if (plan.kind === "refused") {
            // The value never reaches a lookup; a real slug makes a future regression a real leak.
            values[param.name] = param.name === "profile_slug" || param.name === "peer_slug" ? ownerSlug : standIn;
            missing[param.name] = standIn;
            continue;
        }
        const rule = ruleFor(param.name, endpoint.rel);
        switch (rule?.kind) {
            case "owned":
                values[param.name] = await registry.owned(rule.key);
                missing[param.name] = standIn;
                substitutions.push([values[param.name] ?? "", standIn]);
                break;
            case "selector":
                values[param.name] = await registry.selector(rule.key);
                missing[param.name] = values[param.name] ?? "";
                break;
            case "profile":
                values[param.name] = ownerSlug;
                missing[param.name] = standIn;
                substitutions.push([ownerSlug, standIn]);
                break;
            default:
                hasPlaceholder = true;
                values[param.name] = standIn;
                missing[param.name] = standIn;
        }
    }
    return { endpoint, plan, path: fill(endpoint.rel, values), missingPath: fill(endpoint.rel, missing), substitutions, hasPlaceholder };
}

function label(endpoint: Endpoint): string {
    return `${endpoint.method.toUpperCase()} ${endpoint.rel}`;
}

function sameAsMissing(what: string, foreign: Answer, missing: Answer, substitutions: Array<[string, string]>): string | null {
    if (foreign.status !== 404) {
        return `${what} answered ${foreign.status} for another account's object rather than looking absent: ${foreign.body.slice(0, 200)}`;
    }
    if (missing.status !== 404) {
        return `${what}: the missing-object control answered ${missing.status}, so the stranger's 404 proves nothing: ${missing.body.slice(0, 200)}`;
    }
    const normalised = substitutions.reduce((text, [real, fake]) => (real.length >= 4 ? text.split(real).join(fake) : text), foreign.body);
    return normalised === missing.body ? null : `${what}: another account's object and a missing one answer with different bodies, so the id is an oracle:\n    theirs:  ${foreign.body.slice(0, 200)}\n    missing: ${missing.body.slice(0, 200)}`;
}

async function sweep(family: string, deps: { api: ApiClient; secondaryApi: ApiClient; anonymousApi: ApiClient; request: APIRequestContext }): Promise<SweepReport> {
    const { api, secondaryApi, anonymousApi, request } = deps;
    const doc = await loadSchema(anonymousApi);
    const endpoints = endpointsOf(doc).filter((endpoint) => endpoint.params.length > 0 && familyOf(endpoint.rel) === family);
    expect(endpoints.length, `the schema publishes no parameterised endpoint in the "${family}" family; if it was retired, drop the family`).toBeGreaterThan(0);

    const report: SweepReport = { probed: [], skipped: [], unexercised: [], problems: [] };
    const registry = new Registry(api, request, randomMarker("sweep"));
    const writeMarker = randomMarker("sweepw");
    const ownerSlug = (await api.json<{ slug: string }>("get", "whoami/")).slug;

    const probes: Probe[] = [];
    for (const endpoint of endpoints) {
        const plan = classify(endpoint);
        if (plan.kind === "unruled") {
            report.problems.push(`${label(endpoint)}: no sweep rule for ${plan.params.join(", ")}`);
            continue;
        }
        if (plan.kind === "skip") {
            report.skipped.push(`${label(endpoint)}: ${plan.why}`);
            continue;
        }
        try {
            probes.push(await resolveProbe(registry, endpoint, plan, ownerSlug));
        } catch (error) {
            if (error instanceof UnavailableError) {
                report.skipped.push(`${label(endpoint)}: unavailable in this environment - ${error.message}`);
            } else {
                report.problems.push(`${label(endpoint)}: could not build the owner's object - ${(error as Error).message.slice(0, 300)}`);
            }
        }
    }

    // Owner control, and the baseline the post-write re-read is compared with.
    const ownerReads = probes.filter((probe) => probe.endpoint.method === "get" && (probe.plan.kind === "owned" || probe.plan.kind === "profile") && !probe.hasPlaceholder);
    const baseline = new Map<Probe, Answer>();
    await inBatches(ownerReads, 3, async (probe) => {
        const override = OWNER_READS[probe.endpoint.rel];
        const path = override?.via ? fillVia(probe, override.via) : probe.path;
        const answer = await send(request, api, probe.endpoint, path);
        baseline.set(probe, answer);
        const allowed = override?.statuses ?? [200, 201, 202, 204];
        if (!allowed.includes(answer.status)) {
            report.problems.push(`${label(probe.endpoint)}: the owner's own GET ${path} answered ${answer.status}, so the stranger's refusal would prove nothing: ${answer.body.slice(0, 200)}`);
        }
    });

    // Stranger reads.
    const reads = probes.filter((probe) => probe.endpoint.method === "get");
    await inBatches(reads, 3, async (probe) => {
        const what = label(probe.endpoint);
        report.probed.push(what);
        const foreign = await send(request, secondaryApi, probe.endpoint, probe.path);
        if (foreign.status >= 500) {
            report.problems.push(`${what} crashed (${foreign.status}) for a stranger: ${foreign.body.slice(0, 200)}`);
            return;
        }
        switch (probe.plan.kind) {
            case "refused":
                if (foreign.status !== 403) {
                    report.problems.push(`${what} answered ${foreign.status} to an API key; messaging scopes are OAuth2-only: ${foreign.body.slice(0, 200)}`);
                }
                return;
            case "unowned":
                if (foreign.status < 400) {
                    report.problems.push(`${what} answered ${foreign.status} for an id that belongs to nobody: ${foreign.body.slice(0, 200)}`);
                }
                return;
            case "profile":
                if (foreign.status === 200) {
                    // A profile may be visible under its owner's profile_visibility; its privacy settings never are.
                    const visibility = (JSON.parse(foreign.body || "{}") as { visibility?: unknown }).visibility;
                    if (visibility !== null && visibility !== undefined) {
                        report.problems.push(`${what}: a stranger was served the owner's visibility settings`);
                    }
                    return;
                }
                break;
            default:
                break;
        }
        const problem = sameAsMissing(what, foreign, await send(request, secondaryApi, probe.endpoint, probe.missingPath), probe.substitutions);
        if (problem) {
            report.problems.push(problem);
        }
    });

    // Stranger writes: one at a time, deletes last and deepest first so a wrongly accepted delete cannot
    // mask the probes after it.
    const writes = probes
        .filter((probe) => probe.endpoint.method !== "get")
        .sort((a, b) => Number(a.endpoint.method === "delete") - Number(b.endpoint.method === "delete") || b.path.split("/").length - a.path.split("/").length);
    for (const probe of writes) {
        const what = label(probe.endpoint);
        report.probed.push(what);
        // Nothing but an empty body goes at the owner's real profile: a check-first view refuses it, and a
        // broken one changes nothing.
        const body = probe.plan.kind === "profile" ? {} : sampleBody(doc, probe.endpoint.op, writeMarker);
        const foreign = await send(request, secondaryApi, probe.endpoint, probe.path, body);
        const uniform = UNIFORM_ANSWERS[probe.endpoint.rel];
        if (foreign.status >= 500) {
            report.problems.push(`${what} crashed (${foreign.status}) for a stranger: ${foreign.body.slice(0, 200)}`);
        } else if (probe.plan.kind === "refused") {
            if (foreign.status !== 403) {
                report.problems.push(`${what} answered ${foreign.status} to an API key; messaging scopes are OAuth2-only: ${foreign.body.slice(0, 200)}`);
            }
        } else if (uniform) {
            if (!uniform.includes(foreign.status)) {
                report.problems.push(`${what} answered ${foreign.status}; this endpoint answers ${uniform.join("/")} for every id by design`);
            }
        } else if (foreign.status < 400) {
            report.problems.push(`${what} LANDED for a stranger (${foreign.status}): ${foreign.body.slice(0, 200)}`);
        } else if (probe.plan.kind !== "unowned" && foreign.status !== 404) {
            const control = await send(request, secondaryApi, probe.endpoint, probe.missingPath, body);
            if (control.status !== foreign.status) {
                report.problems.push(`${what} answered ${foreign.status} for another account's object and ${control.status} for a missing one, so the id is an oracle: ${foreign.body.slice(0, 200)}`);
            } else {
                report.unexercised.push(`${what}: ${foreign.status} for both, before any lookup - ${foreign.body.slice(0, 120)}`);
            }
        }
    }

    // Owner re-read: nothing the stranger sent is visible, nothing observable moved, nothing is gone.
    await inBatches([...baseline.keys()], 3, async (probe) => {
        const override = OWNER_READS[probe.endpoint.rel];
        const path = override?.via ? fillVia(probe, override.via) : probe.path;
        const after = await send(request, api, probe.endpoint, path);
        const before = baseline.get(probe);
        if (after.body.includes(writeMarker)) {
            report.problems.push(`${label(probe.endpoint)}: the owner now reads text only the stranger ever sent`);
        }
        if (before && !SELF_CHANGING.some((pattern) => pattern.test(probe.endpoint.rel))) {
            const [was, now] = [stateOf(before.body), stateOf(after.body)];
            if (was !== now) {
                report.problems.push(`${label(probe.endpoint)}: the owner's state changed during the stranger's writes:\n    before: ${was.slice(0, 300)}\n    after:  ${now.slice(0, 300)}`);
            }
        }
    });
    for (const check of registry.checks) {
        const problem = await check.run();
        if (problem) {
            report.problems.push(problem);
        }
    }
    return report;
}

function fillVia(probe: Probe, via: string): string {
    // Every parameter in a `via` path also appears in the probed path, so the probe's values carry over.
    const values: Record<string, string> = {};
    const names = [...probe.endpoint.rel.matchAll(/\{([^}]+)\}/g)].map((match) => match[1] ?? "");
    const segments = probe.path.split("/");
    probe.endpoint.rel.split("/").forEach((segment, index) => {
        const name = /^\{([^}]+)\}$/.exec(segment)?.[1];
        if (name && names.includes(name)) {
            values[name] = decodeURIComponent(segments[index] ?? "");
        }
    });
    return fill(via, values);
}

async function publish(report: SweepReport, family: string): Promise<void> {
    const info = test.info();
    for (const skipped of report.skipped.filter((entry) => entry.includes("unavailable in this environment"))) {
        info.annotations.push({ type: "sweep-unavailable", description: skipped });
    }
    await info.attach(`sweep-${family.replace(/[^a-z]+/gi, "-")}.txt`, {
        contentType: "text/plain",
        body: [
            `probed (${report.probed.length}):`,
            ...report.probed.sort().map((line) => `  ${line}`),
            `skipped (${report.skipped.length}):`,
            ...report.skipped.map((line) => `  ${line}`),
            `refused before any lookup, so authorization was not exercised (${report.unexercised.length}):`,
            ...report.unexercised.map((line) => `  ${line}`),
        ].join("\n"),
    });
    expect(report.probed.length, `nothing in "${family}" was probed`).toBeGreaterThan(0);
    expect(report.problems, `the "${family}" sweep found ${report.problems.length} problem(s):\n  ${report.problems.join("\n  ")}`).toEqual([]);
}

// Sequential, not fully parallel: every family draws on the stranger key's 60/minute burst and
// 300/hour write budgets, and a retry would spend the write budget twice.
test.describe.configure({ mode: "default", retries: 0 });

test.describe("every published endpoint treats another account's object as missing", () => {
    test("every path parameter in the published schema has a sweep rule", async ({ anonymousApi }) => {
        const endpoints = endpointsOf(await loadSchema(anonymousApi));
        const parameterised = endpoints.filter((endpoint) => endpoint.params.length > 0);
        expect(parameterised.length, "the schema publishes no parameterised endpoint, so the sweep below is vacuous").toBeGreaterThan(50);

        const gaps: string[] = [];
        for (const endpoint of parameterised) {
            if (familyOf(endpoint.rel) === null) {
                gaps.push(`${label(endpoint)}: no sweep family for "${endpoint.rel.split("/")[0]}" - add it to FAMILY_BY_SEGMENT`);
                continue;
            }
            const plan = classify(endpoint);
            if (plan.kind === "unruled") {
                gaps.push(`${label(endpoint)}: no rule for path parameter(s) ${plan.params.join(", ")} - add a factory to OWNED_BY_PARAM, or a justified entry to PLACEHOLDERS/EXEMPT_PARAMS`);
            }
        }
        expect(gaps, `published endpoints the privacy sweep cannot cover:\n  ${gaps.join("\n  ")}`).toEqual([]);
    });

    for (const family of FAMILIES) {
        ifSecondaryAccount()(`sweep: ${family}`, async ({ api, secondaryApi, anonymousApi, apiRequestContext }) => {
            test.setTimeout(15 * 60_000);
            const report = await sweep(family, { api, secondaryApi, anonymousApi, request: apiRequestContext });
            await publish(report, family);
        });
    }

    ifSecondaryAccount()("no index, feed or search endpoint returns another account's objects", async ({ api, secondaryApi, anonymousApi, apiRequestContext }) => {
        test.setTimeout(10 * 60_000);
        const marker = randomMarker("sweepidx");
        const pin = await api.createPin({ name: `${resourceName("sweep index pin")} ${marker}` });
        const list = await createList(api, `${resourceName("sweep index list")} ${marker}`);
        const trip = await createTrip(api, `${resourceName("sweep index trip")} ${marker}`);
        const labelRow = await createLabel(api, `${resourceName("sweep index label")} ${marker}`);
        const identifiers = [marker, pin.uuid, pin.slug, list.slug, trip.slug, labelRow.uuid];

        // Control: the owner's own search reaches the seeded rows by the marker every probe below sends.
        const mine = await api.json<{ groups?: Array<{ type?: string }> }>("get", "search/", { q: marker });
        for (const type of ["pins", "trips"]) {
            const group = JSON.stringify((mine.groups ?? []).find((candidate) => candidate.type === type) ?? {});
            expect(group, `the owner's search misses their own ${type}, so the stranger's miss would prove nothing`).toContain(marker);
        }

        const endpoints = endpointsOf(await loadSchema(anonymousApi)).filter(
            (endpoint) => endpoint.method === "get" && endpoint.params.length === 0 && !["schema/", "docs/"].includes(endpoint.rel),
        );
        expect(endpoints.length, "the schema publishes no parameterless GET, so this test is vacuous").toBeGreaterThan(20);

        const leaks: string[] = [];
        await inBatches(endpoints, 3, async (endpoint) => {
            const takesQuery = (endpoint.op.parameters ?? []).some((parameter) => parameter.in === "query" && parameter.name === "q");
            const path = takesQuery ? `${endpoint.rel}?q=${encodeURIComponent(marker)}&sources=local` : endpoint.rel;
            const answer = await send(apiRequestContext, secondaryApi, endpoint, path);
            if (answer.status >= 500) {
                leaks.push(`GET ${path} crashed (${answer.status}): ${answer.body.slice(0, 160)}`);
                return;
            }
            // `query` echoes the search term verbatim by design; only result rows count.
            let body = answer.body;
            try {
                const parsed = JSON.parse(answer.body) as Record<string, unknown>;
                delete parsed.query;
                body = JSON.stringify(parsed);
            } catch {
                // Not JSON: compared as-is.
            }
            const found = identifiers.filter((identifier) => body.includes(identifier));
            if (found.length > 0) {
                leaks.push(`GET ${path} (${answer.status}) returned another account's ${found.join(", ")}`);
            }
        });
        expect(leaks, `index and search endpoints leaked another account's objects:\n  ${leaks.join("\n  ")}`).toEqual([]);
    });
});
