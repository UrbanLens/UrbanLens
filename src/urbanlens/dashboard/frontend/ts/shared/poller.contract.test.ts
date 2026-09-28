/**
 * A bare setInterval keeps running in a hidden tab and after an htmx swap removes what it serves; polls go through startPoller.
 */
import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const DASHBOARD = join(import.meta.dir, "../../..");

/** Intervals that are not polls, each cleared by its owner. */
const ALLOWED: Record<string, string> = {
    "frontend/ts/shared/live-socket.ts": "WebSocket heartbeat, cleared on close",
    "templates/dashboard/partials/safety/_chat_panel.html": "WebSocket heartbeat, cleared on close",
    "frontend/ts/entries/spotguessr.ts": "one round's countdown, cleared when the round ends",
};

function walk(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path, out);
        else if ((name.endsWith(".ts") && !name.includes(".test.")) || name.endsWith(".html")) out.push(path);
    }
    return out;
}

describe("background polling", () => {
    test("no page or module polls with a bare setInterval", () => {
        const offenders = [...walk(join(DASHBOARD, "frontend/ts")), ...walk(join(DASHBOARD, "templates"))]
            .filter((path) => /\bsetInterval\(/.test(readFileSync(path, "utf8")))
            .map((path) => relative(DASHBOARD, path))
            .filter((path) => !(path in ALLOWED));
        expect(offenders).toEqual([]);
    });

    test("every allowance still names a file that uses setInterval", () => {
        for (const path of Object.keys(ALLOWED)) {
            expect(readFileSync(join(DASHBOARD, path), "utf8")).toMatch(/\bsetInterval\(/);
        }
    });
});
