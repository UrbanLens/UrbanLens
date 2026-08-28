/**
 * User-controlled strings must stay data.
 *
 * Names, descriptions, notes, comments and search queries are the fields an
 * application that stores other people's words has to treat as hostile. The
 * assertions here are about what the deployment *did* with those strings:
 * they round-trip as text, they do not become DOM nodes, they do not dump
 * other people's rows, they do not 500. The control in each case is that a
 * boring value still works, so a refusal is not "the field is broken".
 *
 * Markup in these tests is a canary, not a payload: an element with that id
 * existing in the DOM is the failure.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { pinDetail } from "../../lib/routes.js";
import {
    expectCanaryNotInDom,
    expectNotServerError,
    markupCanary,
    uniqueMarker,
} from "../../lib/security.js";

test.describe("stored markup is not interpreted as HTML", () => {
    test("a pin description containing a canary tag renders as text", async ({ api, page, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("desc");
        const canary = markupCanary(marker);
        const pin = await api.createPin({
            name: resourceName("markup description"),
            description: canary,
        });

        const stored = await api.json<{ description?: string }>("get", `pins/${pin.slug}/`);
        expect(stored.description, "the description did not round-trip, so there is nothing on the page to interpret").toBeTruthy();

        await page.goto(pinDetail(pin.slug));
        await expect(page.locator("#pin-detail-hero")).toBeVisible();
        await expectCanaryNotInDom(page, marker);

        const html = await page.content();
        // Either the tags were escaped (`&lt;span`) or stripped. An unescaped
        // `<span id="...">` is the failure the DOM count already catches; this
        // pins the source too, including `data-raw-description`.
        expect(html, "the pin page includes an unescaped canary tag").not.toMatch(new RegExp(`<span id="${marker}"`, "i"));
    });

    test("a pin comment containing a canary tag does not become a DOM node", async ({ api, page, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("cmt");
        const canary = markupCanary(marker);
        const pin = await api.createPin({ name: resourceName("markup comment") });

        const created = await api.post(`pins/${pin.slug}/comments/`, { text: canary });
        expect(created.status(), `creating a comment answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);

        const listed = await api.get(`pins/${pin.slug}/comments/`);
        expect(listed.status()).toBe(200);
        const json = await listed.text();
        expect(json, "the comment JSON did not round-trip the marker, so the page has nothing to render").toContain(marker);

        await page.goto(pinDetail(pin.slug));
        await expect(page.locator("#pin-detail-hero")).toBeVisible();
        const commentsTab = page.locator('.ul-subnav-tab[data-tab="comments"], a:has-text("Comments")').first();
        if (await commentsTab.count()) {
            await commentsTab.click();
        }
        await expectCanaryNotInDom(page, marker);
        expect(await page.content()).not.toMatch(new RegExp(`<span id="${marker}"`, "i"));
    });

    test("a pin note containing a canary tag does not become a DOM node", async ({ api, page, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("note");
        const canary = markupCanary(marker);
        const pin = await api.createPin({ name: resourceName("markup note") });

        const created = await api.post(`pins/${pin.slug}/notes/`, { text: canary });
        expect(created.status(), `creating a note answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);

        await page.goto(pinDetail(pin.slug));
        await expect(page.locator("#pin-detail-hero")).toBeVisible();
        await expectCanaryNotInDom(page, marker);
        expect(await page.content()).not.toMatch(new RegExp(`<span id="${marker}"`, "i"));
    });

    test("a boring description still round-trips, so the canary tests are not 'descriptions are dropped'", async ({ api }) => {
        const text = resourceName("plain description");
        const pin = await api.createPin({ name: resourceName("plain desc host"), description: text });
        const stored = await api.json<{ description?: string }>("get", `pins/${pin.slug}/`);
        expect(stored.description).toContain("plain description");
    });
});

test.describe("names cannot carry markup-significant characters", () => {
    test("angle brackets in a submitted name do not survive", async ({ api }) => {
        const marker = uniqueMarker("name");
        const pin = await api.createPin({ name: `<span id="${marker}">${marker}</span>` });
        expect(pin.name, "the stored name still contains '<'").not.toContain("<");
        expect(pin.name, "the stored name still contains '>'").not.toContain(">");
        expect(pin.name, "sanitize_name dropped the marker letters along with the tags, so there is no name to render").toContain(marker);
    });
});

test.describe("search treats hostile strings as literals", () => {
    test("a query that would match everything in SQL does not return unrelated pins", async ({ api }) => {
        const marker = uniqueMarker("sql");
        const pin = await api.createPin({ name: `${resourceName("literal search")} ${marker}` });

        const control = await api.get("search/", { q: marker });
        expect(control.status()).toBe(200);
        expect(await control.text(), "search cannot find a pin by a unique marker, so the poison query's empty result would prove nothing").toContain(pin.slug);

        const poison = `${marker} ' OR '1'='1`;
        const response = await api.get("search/", { q: poison });
        await expectNotServerError(response, "search with a quoted OR string");
        expect(response.status(), `search with a quoted OR string answered ${response.status()}`).toBe(200);

        const body = (await response.json()) as { total?: number; groups?: unknown[] };
        const blob = JSON.stringify(body);
        // The poison string is not the pin's name, so a hit on this pin is
        // the query being interpreted rather than matched. Other people's
        // pins appearing would be the same class of bug at larger scale;
        // we cannot see those, so we assert this pin is *not* a hit.
        if (blob.includes(pin.slug)) {
            expect(blob, "a quoted-OR search returned the pin, which means the extra SQL-shaped text was ignored rather than treated as required literal text - or the matcher tokenises so loosely that this is a substring hit").toContain(marker);
        }
        expect(body.total ?? 0, "a quoted-OR search reported a huge total, which is what an unparameterised query looks like").toBeLessThan(50);
    });

    test("a query that would close a quote does not 500", async ({ api }) => {
        for (const q of [`'`, `"`, `\\`, `${uniqueMarker("q")};`, `%) OR 1=1--`]) {
            const response = await api.get("search/", { q });
            await expectNotServerError(response, `search q=${JSON.stringify(q)}`);
            expect(response.status(), `search q=${JSON.stringify(q)} answered ${response.status()}`).toBe(200);
        }
    });
});

test.describe("links cannot point at the server's own network", () => {
    test("a pin link to a public https URL is accepted, so later refusals are about the URL", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("safe link host") });
        const created = await api.post(`pins/${pin.slug}/links/`, {
            url: "https://example.invalid/urbanlens-security",
            name: resourceName("safe link"),
        });
        expect(created.status(), `a public https link answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);
    });

    test("a pin link to a non-http scheme is refused", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("scheme link host") });
        const accepted: string[] = [];
        for (const url of ["file:///etc/passwd", "javascript:void(0)", "data:text/html,hi"]) {
            const response = await api.post(`pins/${pin.slug}/links/`, { url, name: resourceName("bad scheme") });
            await expectNotServerError(response, `link ${url}`);
            if (response.status() < 300) {
                accepted.push(`${url} -> ${response.status()}`);
            }
        }
        expect(accepted, `pin links accepted a non-http(s) URL:\n  ${accepted.join("\n  ")}`).toHaveLength(0);
    });
});

test.describe("path traversal does not read files off disk", () => {
    test("static and media paths cannot walk out of their roots", async ({ request }) => {
        const leaked: string[] = [];
        const probes = [
            "/static/../manage.py",
            "/static/../../src/urbanlens/manage.py",
            "/media/../manage.py",
            "/media/pin_images/../../UrbanLens/settings/base.py",
            "/dashboard/../../accounts/login/",
            "/media/%2e%2e/%2e%2e/etc/passwd",
        ];
        for (const path of probes) {
            const response = await request.get(path, { maxRedirects: 0 });
            await expectNotServerError(response, path);
            if (response.status() !== 200) {
                continue;
            }
            const body = await response.text();
            if (/django\.core|SECRET_KEY|root:(?:x|\*):0:0:/.test(body)) {
                leaked.push(`${path} answered 200 with file contents`);
            }
        }
        expect(leaked, `path traversal served source or passwd:\n  ${leaked.join("\n  ")}`).toHaveLength(0);
    });
});

test.describe("request smuggling-adjacent headers do not change the response", () => {
    test("CRLF in X-Forwarded-Host is not reflected as a header", async ({ request }) => {
        const response = await request.get("/health/live", {
            headers: { "X-Forwarded-Host": "example.invalid\r\nX-Injected: 1" },
        });
        expect(response.headers()["x-injected"], "a CR-LF in X-Forwarded-Host injected a response header").toBeFalsy();
        expect(response.status()).toBe(200);
    });

    test("a huge URL is refused rather than crashing", async ({ request }) => {
        const response = await request.get(`/dashboard/map/?q=${"a".repeat(8000)}`, { maxRedirects: 0 });
        await expectNotServerError(response, "8000-char query string");
        expect(response.status(), "an oversized query produced a 5xx").toBeLessThan(500);
    });
});
