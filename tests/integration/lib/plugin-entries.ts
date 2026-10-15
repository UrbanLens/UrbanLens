/**
 * What package plugins add to this suite: `src/urbanlens/plugins/<name>/integration/entries.json`, when a plugin ships one.
 *
 * Core's suite names nothing a plugin owns. A plugin's routes, sweep families and parameter placeholders live beside the
 * plugin, so a tree without the plugin has none of them, and a tree with it sweeps them like core's own.
 *
 * An unknown key throws. A plugin whose suite is incomplete without a contribution lists it under `requires`
 * (`"requires": ["unsignedPosts"]`), and the load throws when the file does not provide it.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export interface OptionalRoute {
    name: string;
    path: string;
    gate: string;
}

/** An endpoint that must refuse a POST without a valid signature, so an anonymous caller cannot forge what it accepts. */
export interface UnsignedPost {
    name: string;
    path: string;
    body: Record<string, unknown>;
    /** What an accepted forged POST would let someone do, for the failure message. */
    risk: string;
}

export interface PluginEntries {
    /** Pages that may legitimately be switched off; see `optionalRoutes` in routes.ts. */
    optionalRoutes: OptionalRoute[];
    /** Endpoints that must refuse an unsigned POST; see the security surfaces spec. */
    unsignedPosts: UnsignedPost[];
    /** Top-level API path segment -> sweep family name, for schema-sweep's FAMILY_BY_SEGMENT. */
    sweepSegments: Record<string, string>;
    /** Path parameter -> why the sweep cannot mint a real owner-side value, for schema-sweep's PLACEHOLDERS. */
    sweepPlaceholders: Record<string, string>;
}

const PLUGINS_DIR = join(dirname(fileURLToPath(import.meta.url)), "../../../src/urbanlens/plugins");

/** What a plugin's entries.json may contribute. A key outside this set is a typo that would otherwise drop silently. */
const CONTRIBUTIONS = ["optionalRoutes", "unsignedPosts", "sweepSegments", "sweepPlaceholders"] as const;
type Contribution = (typeof CONTRIBUTIONS)[number];

/** A plugin may also list, under `requires`, the contributions its suite is incomplete without. */
const KEYS: readonly string[] = [...CONTRIBUTIONS, "requires"];

const LIST_CONTRIBUTIONS: ReadonlySet<string> = new Set(["optionalRoutes", "unsignedPosts"]);

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Throws unless every entry of `list` is an object whose `fields` are all non-empty strings. */
function checkEntries(source: string, key: string, list: unknown[], fields: readonly string[]): void {
    list.forEach((item, index) => {
        for (const field of fields) {
            if (!isRecord(item) || typeof item[field] !== "string" || item[field] === "") {
                throw new Error(`${source}: ${key}[${index}] needs a non-empty string "${field}"`);
            }
        }
    });
}

/** Parses one plugin's entries.json, throwing on an unknown key, a wrong shape, or a required contribution it lacks. */
export function parseEntries(source: string, text: string): Partial<PluginEntries> {
    const found: unknown = JSON.parse(text);
    if (!isRecord(found)) throw new Error(`${source}: expected a JSON object`);
    for (const key of Object.keys(found)) {
        if (!KEYS.includes(key)) throw new Error(`${source}: unknown key "${key}"; the keys are ${KEYS.join(", ")}`);
    }
    for (const key of CONTRIBUTIONS) {
        const value = found[key];
        if (value === undefined) continue;
        const wanted = LIST_CONTRIBUTIONS.has(key) ? Array.isArray(value) : isRecord(value);
        if (!wanted) throw new Error(`${source}: "${key}" must be ${LIST_CONTRIBUTIONS.has(key) ? "an array" : "an object"}`);
    }
    checkEntries(source, "optionalRoutes", (found["optionalRoutes"] as unknown[] | undefined) ?? [], ["name", "path", "gate"]);
    checkEntries(source, "unsignedPosts", (found["unsignedPosts"] as unknown[] | undefined) ?? [], ["name", "path", "risk"]);
    const required = found["requires"] ?? [];
    if (!Array.isArray(required) || required.some((key) => !CONTRIBUTIONS.includes(key as Contribution))) {
        throw new Error(`${source}: "requires" must list some of ${CONTRIBUTIONS.join(", ")}`);
    }
    for (const key of required as Contribution[]) {
        const value = found[key];
        if (value === undefined || (Array.isArray(value) ? value.length === 0 : Object.keys(value as object).length === 0)) {
            throw new Error(`${source}: requires "${key}", which it does not provide`);
        }
    }
    return found as Partial<PluginEntries>;
}

function load(): PluginEntries {
    const merged: PluginEntries = { optionalRoutes: [], unsignedPosts: [], sweepSegments: {}, sweepPlaceholders: {} };
    if (!existsSync(PLUGINS_DIR)) return merged;
    for (const entry of readdirSync(PLUGINS_DIR, { withFileTypes: true })) {
        const file = join(PLUGINS_DIR, entry.name, "integration/entries.json");
        if (!entry.isDirectory() || !existsSync(file)) continue;
        const found = parseEntries(`${entry.name}/integration/entries.json`, readFileSync(file, "utf8"));
        merged.optionalRoutes.push(...(found.optionalRoutes ?? []));
        merged.unsignedPosts.push(...(found.unsignedPosts ?? []));
        Object.assign(merged.sweepSegments, found.sweepSegments ?? {});
        Object.assign(merged.sweepPlaceholders, found.sweepPlaceholders ?? {});
    }
    return merged;
}

export const pluginEntries: PluginEntries = load();
