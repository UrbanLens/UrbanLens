/**
 * Keeps the templates off the htmx features that need `'unsafe-eval'`, which the enforced CSP
 * refuses, and keeps every declared request action backed by a registration.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { BUILTIN_ACTION_NAMES } from "./htmx-actions";

const DASHBOARD = join(import.meta.dir, "../../..");

function filesUnder(directory: string, extensions: string[]): string[] {
    const found: string[] = [];
    for (const entry of readdirSync(directory)) {
        if (entry === "node_modules" || entry === "static" || entry === "dashboard.egg-info") continue;
        const path = join(directory, entry);
        if (statSync(path).isDirectory()) found.push(...filesUnder(path, extensions));
        else if (extensions.some((ext) => entry.endsWith(ext)) && !entry.includes(".test.")) found.push(path);
    }
    return found;
}

const templates = filesUnder(join(DASHBOARD, "templates"), [".html"]).map((path) => ({ path, source: readFileSync(path, "utf8") }));
const scripts = [
    ...filesUnder(join(DASHBOARD, "frontend/ts"), [".ts"]),
    ...readdirSync(join(DASHBOARD, "frontend/static/js"))
        .filter((name) => name.endsWith(".js"))
        .map((name) => join(DASHBOARD, "frontend/static/js", name)),
].map((path) => ({ path, source: readFileSync(path, "utf8") }));

describe("htmx features that evaluate strings", () => {
    test("the scan reads the templates", () => {
        expect(templates.length).toBeGreaterThan(100);
    });

    test("no hx-on handler", () => {
        for (const { path, source } of templates) {
            expect({ path, hxOn: /\bhx-on[:=-]/.test(source) }).toEqual({ path, hxOn: false });
        }
    });

    test("no hx-vars, and no hx-vals evaluated as script", () => {
        for (const { path, source } of templates) {
            expect({ path, evaluated: /\bhx-vars\s*=|\bhx-vals\s*=\s*["']\s*(js|javascript):/.test(source) }).toEqual({ path, evaluated: false });
        }
    });

    test("no hx-trigger event filter", () => {
        for (const { path, source } of templates) {
            for (const [, spec] of source.matchAll(/hx-trigger\s*=\s*"([^"]*)"/g)) {
                // A filter is a `[` straight after an event name; `from:[name=x]` is a selector.
                const filtered = /(^|[\s,])[\w:.-]+\[/.test(spec!.replace(/from:\S+/g, ""));
                expect({ path, spec, filtered }).toEqual({ path, spec, filtered: false });
            }
        }
    });
});

describe("declared request actions", () => {
    const registered = new Set<string>();
    for (const { source } of [...templates, ...scripts]) {
        for (const [, name] of source.matchAll(/ulHtmxActions\??\.register\(\s*["']([\w-]+)["']/g)) registered.add(name!);
    }

    const used: { path: string; name: string }[] = [];
    for (const { path, source } of templates) {
        for (const [, value] of source.matchAll(/data-ul-(?:before-request|after-request|on-success)="([^"]*)"/g)) {
            const rendered = value!.replace(/\{\{.*?\}\}/g, "x").replace(/\{%.*?%\}/g, "");
            for (const token of rendered.split(/\s+/).filter(Boolean)) used.push({ path, name: token.split(":")[0]! });
        }
    }

    test("the scan finds the declarations it is meant to guard", () => {
        expect(used.length).toBeGreaterThan(30);
        expect(registered.size).toBeGreaterThan(3);
    });

    test("every action a template names is built in or registered somewhere", () => {
        for (const { path, name } of used) {
            expect({ path, name, known: BUILTIN_ACTION_NAMES.includes(name) || registered.has(name) }).toEqual({ path, name, known: true });
        }
    });
});
