/**
 * Shared assertions for the security project.
 *
 * These tests exist to fail when a future change opens a door: a private row
 * becoming visible, a credential appearing in HTML, a markup string being
 * interpreted as a DOM node. Every interesting check has a *control* sitting
 * next to it - the owner can still read their own row, the deployment still
 * answers, the restricted key is still valid - so a green run cannot be
 * explained by "everything 404s" or "the suite pointed at a dead host".
 *
 * The suite talks to the deployment the same way a client does. It does not
 * ship scanners, payload packs, or exploit tooling: a refusal, an identical
 * 404 pair, and a missing DOM node are the evidence.
 */

import { expect, type APIResponse, type Browser, type BrowserContext, type Page } from "@playwright/test";

import type { ApiClient } from "./api-client.js";
import { env } from "./env.js";

/** A slug that will never collide with a real object this suite created. */
export const MISSING_SLUG = "definitely-not-a-real-resource-91b2c";

/** RFC 4122-shaped, and reserved so it cannot belong to a row we minted. */
export const MISSING_UUID = "00000000-0000-4000-8000-000000000000";

/**
 * A short token unique to this run, safe as an HTML id and as a search string.
 *
 * Letters first so it is a valid `id="..."` selector. No markup-significant
 * characters: those belong in the canary *payload*, not in the identifier
 * used to find it afterwards.
 */
export function uniqueMarker(label = ""): string {
    const run = env.runId.replace(/[^a-zA-Z0-9]/g, "").slice(-10);
    const suffix = label.replace(/[^a-zA-Z0-9]/g, "").slice(0, 12);
    return `ulsec${run}${suffix}`;
}

/** Markup that, if interpreted as HTML, produces an element we can count. */
export function markupCanary(marker: string): string {
    return `<span id="${marker}">${marker}</span>`;
}

/** A browser context with no cookies and no stored origins. */
export async function anonymousContext(browser: Browser): Promise<BrowserContext> {
    return browser.newContext({
        baseURL: env.baseUrl,
        storageState: { cookies: [], origins: [] },
        ignoreHTTPSErrors: env.ignoreHttpsErrors,
    });
}

export function isAuthRefusal(status: number): boolean {
    return status === 401 || status === 403;
}

export function isAbsence(status: number): boolean {
    return status === 404;
}

export function locationHeader(response: APIResponse): string {
    return response.headers().location ?? response.headers()["Location"] ?? "";
}

export function redirectedToLogin(response: APIResponse): boolean {
    const status = response.status();
    if (status !== 302 && status !== 301 && status !== 303 && status !== 307 && status !== 308) {
        return false;
    }
    return locationHeader(response).includes("/accounts/login");
}

/**
 * A protected resource was not handed over.
 *
 * 401/403 (not allowed), 404 (indistinguishable from missing), or a
 * login redirect are all refusals. 2xx is not, even when the body is an
 * "error" fragment - a 200 that names the object still confirms it exists.
 */
export function wasRefused(response: APIResponse): boolean {
    const status = response.status();
    return isAuthRefusal(status) || isAbsence(status) || redirectedToLogin(response);
}

export async function expectRefused(response: APIResponse, what: string): Promise<void> {
    expect(
        wasRefused(response),
        `${what} was served (${response.status()}). A private resource has to be refused, redirected to sign-in, or look like it does not exist.`,
    ).toBeTruthy();
}

export async function expectNotServerError(response: APIResponse, what: string): Promise<void> {
    expect(
        response.status(),
        `${what} answered ${response.status()}, which is a crash rather than a refusal. Body: ${(await response.text()).slice(0, 300)}`,
    ).toBeLessThan(500);
}

/**
 * Another user's object and an object that never existed must be the same answer.
 *
 * A 403 on the real one and a 404 on the fake one is an oracle: the caller
 * learns the real object exists. The bodies have to match too, not just the
 * status - a different error string is the same leak in slower motion.
 */
export async function expectIndistinguishableFromMissing(
    foreign: APIResponse,
    missing: APIResponse,
    what: string,
): Promise<void> {
    expect(foreign.status(), `${what} answered ${foreign.status()} rather than looking absent`).toBe(404);
    expect(missing.status(), `the missing-object control for ${what} answered ${missing.status()} rather than 404`).toBe(404);
    expect(
        await foreign.text(),
        `${what}: the answer for a real object you cannot reach differs from the answer for one that never existed, so a slug/uuid is an oracle`,
    ).toBe(await missing.text());
}

/** Substrings that mean Django is running with DEBUG, or a stack leaked. */
export const DEBUG_LEAKS: ReadonlyArray<{ name: string; pattern: RegExp }> = [
    { name: "DEBUG = True banner", pattern: /You're seeing this error because you have DEBUG/i },
    { name: "Django URL debug", pattern: /Django tried these URL patterns/i },
    { name: "Python traceback", pattern: /Traceback \(most recent call last\)/ },
    { name: "SECRET_KEY", pattern: /SECRET_KEY/ },
    { name: "field encryption key", pattern: /UL_FIELD_ENCRYPTION_KEY/ },
    { name: "database DSN", pattern: /postgres(?:ql)?:\/\//i },
    { name: "exception module path", pattern: /django\.core\.exceptions/ },
];

export async function expectNoDebugLeak(body: string, where: string): Promise<void> {
    for (const leak of DEBUG_LEAKS) {
        expect(body, `${where} leaked ${leak.name}`).not.toMatch(leak.pattern);
    }
}

/**
 * Paths that must never serve their real contents on a deployment.
 *
 * A 404/403/401 is success. A 200 is only a failure when the body actually
 * matches the signature - a branded 404 page that happens to be 200 would
 * otherwise fail every run for the wrong reason.
 */
export const SENSITIVE_PATHS: ReadonlyArray<{ path: string; name: string; leak: RegExp }> = [
    { path: "/.git/HEAD", name: ".git/HEAD", leak: /^ref:\s/ },
    { path: "/.git/config", name: ".git/config", leak: /\[core\]/ },
    { path: "/.env", name: ".env", leak: /^(?:UL_|DJANGO_|SECRET_|DATABASE_|UL_FIELD_)/m },
    { path: "/.env.local", name: ".env.local", leak: /^(?:UL_|DJANGO_|SECRET_|DATABASE_)/m },
    { path: "/.env.production", name: ".env.production", leak: /^(?:UL_|DJANGO_|SECRET_|DATABASE_)/m },
    { path: "/docker-compose.yml", name: "docker-compose.yml", leak: /(?:services:\s|django:)/ },
    { path: "/compose.yml", name: "compose.yml", leak: /services:\s/ },
    { path: "/pyproject.toml", name: "pyproject.toml", leak: /\[project\]/ },
    { path: "/manage.py", name: "manage.py", leak: /django\.core/ },
    { path: "/src/urbanlens/manage.py", name: "src manage.py", leak: /django\.core/ },
    { path: "/debug/", name: "debug/", leak: /DEBUG/ },
    { path: "/__debug__/", name: "Django Debug Toolbar", leak: /djdt|debug.?toolbar/i },
    { path: "/__debug__/history/", name: "Django Debug Toolbar history", leak: /djdt|debug.?toolbar/i },
    { path: "/server-status", name: "Apache server-status", leak: /Apache\s+Server\s+Status/i },
    { path: "/nginx_status", name: "nginx stub_status", leak: /Active connections/i },
    { path: "/actuator/env", name: "Spring actuator env", leak: /"propertySources"/ },
    { path: "/phpinfo.php", name: "phpinfo", leak: /phpinfo\(\)/ },
    { path: "/.DS_Store", name: ".DS_Store", leak: /^\0\x05Bud1/ },
    { path: "/backup.sql", name: "backup.sql", leak: /(?:CREATE TABLE|COPY public\.)/i },
    { path: "/dump.sql", name: "dump.sql", leak: /(?:CREATE TABLE|COPY public\.)/i },
    { path: "/web.config", name: "web.config", leak: /<configuration>/i },
    { path: "/crossdomain.xml", name: "crossdomain.xml", leak: /<cross-domain-policy/i },
    { path: "/proc/self/environ", name: "proc environ", leak: /PATH=/ },
];

/** Directory indexes must not list files. */
export const DIRECTORY_PROBES = ["/static/", "/media/", "/dashboard/static/", "/.git/"] as const;

/**
 * The caller's own profile, via whoami.
 *
 * Used as the control that a credential is valid, so a later 401 cannot be
 * mistaken for a working authorization check.
 */
export async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

export async function expectCanaryNotInDom(page: Page, marker: string): Promise<void> {
    expect(
        await page.locator(`#${marker}`).count(),
        `stored markup was interpreted as HTML: an element with id="${marker}" is in the DOM`,
    ).toBe(0);
}

/**
 * True when `haystack` contains the other account's unique marker.
 *
 * Used to assert that a list/search/map payload scoped to the caller did not
 * pick up a row that belongs to somebody else. Case-sensitive: these markers
 * are random alphanumerics, not English.
 */
export function containsMarker(haystack: string, marker: string): boolean {
    return haystack.includes(marker);
}

export function header(response: { headers: () => Record<string, string> }, name: string): string {
    const headers = response.headers();
    return headers[name.toLowerCase()] ?? "";
}
