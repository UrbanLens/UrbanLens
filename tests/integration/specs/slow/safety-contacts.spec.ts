/**
 * Emergency contacts on a safety check-in (GOALS "Safety check-ins"): no live location by default,
 * only the plan, and only once the check-in is missed; access is by token link.
 *
 * The sharer opens the check-in with the sharee as a contact. Escalation needs the 15-minute grace
 * floor plus a 5-minute beat sweep, so the incident test waits ~20 minutes.
 */

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { requireAccount, SHAREE_ROLE } from "../../lib/accounts.js";
import { env, resourceName } from "../../lib/env.js";
import { ensureFriends } from "../../lib/friendship.js";
import type { ApiClient } from "../../lib/api-client.js";

interface Checkin {
    uuid: string;
    slug: string;
    status: string;
    escalated_at: string | null;
    contacts?: Array<{ username: string | null; notified_at: string | null }>;
}

interface Notification {
    url: string;
    title: string;
    message: string;
}

const GRACE_SECONDS = 900;
const PORTAL_PATH = /\/dashboard\/safety\/contact\/([0-9a-f-]{36})\//;

function marker(label: string): string {
    return `${label}-${Math.random().toString(36).slice(2, 10)}`;
}

async function clearActiveCheckins(api: ApiClient): Promise<void> {
    const page = await api.json<{ results: Checkin[] }>("get", "safety/checkins/", { status: "active", page_size: "100" });
    for (const checkin of page.results) {
        await api.post(`safety/checkins/${checkin.slug}/cancel/`);
        await api.delete(`safety/checkins/${checkin.slug}/`);
    }
}

async function openCheckin(api: ApiClient, title: string, plan: string, deadlineMs: number): Promise<Checkin> {
    const checkin = await api.json<Checkin>("post", "safety/checkins/", {
        title,
        checkin_by: new Date(Date.now() + deadlineMs).toISOString(),
        grace_period_seconds: GRACE_SECONDS,
        plan_details: plan,
        contact_message: `${plan} message`,
        contacts: [{ username: requireAccount(SHAREE_ROLE).username }],
    });
    api.track("safety-checkin", checkin.slug, async () => {
        await api.post(`safety/checkins/${checkin.slug}/cancel/`);
        return api.delete(`safety/checkins/${checkin.slug}/`);
    });
    return checkin;
}

/** Notifications that hand the contact a portal link for the check-in titled `title`. */
async function portalLinks(api: ApiClient, title: string): Promise<string[]> {
    const feed = await api.json<{ results: Notification[] }>("get", "notifications/");
    return feed.results.filter((entry) => PORTAL_PATH.test(entry.url) && `${entry.title} ${entry.message}`.includes(title)).map((entry) => entry.url);
}

test.describe.serial("safety check-in contacts", () => {
    test.beforeEach(async ({ sharerApi, shareeApi }) => {
        await ensureFriends(sharerApi, shareeApi);
        await clearActiveCheckins(sharerApi);
    });

    ifSharingPair()("a contact is told nothing and can read nothing before the check-in is missed", async ({ sharerApi, shareeApi }) => {
        const title = resourceName(marker("contact-quiet"));
        const checkin = await openCheckin(sharerApi, title, marker("plan"), 3 * 3_600_000);

        const owned = await sharerApi.json<Checkin>("get", `safety/checkins/${checkin.slug}/`);
        expect(owned.contacts?.map((contact) => contact.username), "the sharee was not recorded as a contact").toContain(requireAccount(SHAREE_ROLE).username);
        expect(owned.contacts?.every((contact) => contact.notified_at === null), "a contact was notified before any deadline passed").toBe(true);

        expect(await portalLinks(shareeApi, title), "the contact received a portal link before the check-in was missed").toEqual([]);
        const partnered = await shareeApi.json<{ results: Array<{ uuid: string }> }>("get", "safety/partner-checkins/");
        expect(partnered.results.map((row) => row.uuid), "being a contact made the sharee a partner").not.toContain(checkin.uuid);
        expect((await shareeApi.get(`safety/checkins/${checkin.slug}/`)).status()).toBe(404);
        expect((await shareeApi.get(`safety/checkins/${checkin.slug}/location/`)).status()).toBe(404);
    });

    ifSharingPair()("live location is off by default and refused until the owner turns it on", async ({ sharerApi, shareeApi }) => {
        const checkin = await openCheckin(sharerApi, resourceName(marker("contact-live")), marker("plan"), 3 * 3_600_000);
        const fix = { latitude: 12.345678, longitude: -23.456789 };

        const initial = await sharerApi.json<{ sharing_enabled: boolean; latitude: number | null }>("get", `safety/checkins/${checkin.slug}/location/`);
        expect(initial.sharing_enabled, "live location sharing is on for a check-in that never asked for it").toBe(false);
        expect(initial.latitude).toBeNull();

        const refused = await sharerApi.patch(`safety/checkins/${checkin.slug}/location/`, fix);
        expect(refused.status(), `a position was accepted with sharing off: ${(await refused.text()).slice(0, 200)}`).toBe(400);

        const enabled = await sharerApi.patch(`safety/checkins/${checkin.slug}/location/`, { sharing_enabled: true, ...fix });
        expect(enabled.status(), `turning sharing on answered ${enabled.status()}`).toBe(200);
        expect(((await enabled.json()) as { latitude: number | null }).latitude).toBeCloseTo(fix.latitude, 4);

        // Opting in shares with partners; a contact still sees nothing.
        expect((await shareeApi.get(`safety/checkins/${checkin.slug}/location/`)).status()).toBe(404);
    });

    ifSharingPair()("a missed check-in hands the contact a token link showing the plan and nothing live", async ({ sharerApi, shareeApi, playwright }) => {
        const title = resourceName(marker("contact-missed"));
        const plan = marker("plan");
        const checkin = await openCheckin(sharerApi, title, plan, 90_000);

        await expect
            .poll(async () => (await portalLinks(shareeApi, title)).length, {
                message: "the contact was never sent a portal link for a missed check-in",
                timeout: (GRACE_SECONDS + 15 * 60) * 1000,
                intervals: [30_000],
            })
            .toBeGreaterThan(0);

        const escalated = await sharerApi.json<Checkin>("get", `safety/checkins/${checkin.slug}/`);
        expect(escalated.escalated_at, "the contact was notified but the check-in does not record an escalation").not.toBeNull();

        const [link] = await portalLinks(shareeApi, title);
        const token = PORTAL_PATH.exec(link ?? "")?.[1] ?? "";
        const tampered = `${token.slice(0, -1)}${token.endsWith("0") ? "1" : "0"}`;

        const anonymous = await playwright.request.newContext({ baseURL: env.baseUrl, ignoreHTTPSErrors: env.ignoreHttpsErrors });
        try {
            const portal = await anonymous.get(`/dashboard/safety/contact/${token}/`);
            expect(portal.status(), "the token link does not open for someone who is not signed in").toBe(200);
            const html = await portal.text();
            expect(html, "the portal does not show the plan").toContain(plan);

            expect((await anonymous.get(`/dashboard/safety/contact/${tampered}/`)).status(), "a guessed token opened a check-in").toBe(404);
        } finally {
            await anonymous.dispose();
        }

        // The incident gives the contact the plan, not the check-in or a position.
        const live = await sharerApi.json<{ sharing_enabled: boolean }>("get", `safety/checkins/${checkin.slug}/location/`);
        expect(live.sharing_enabled, "escalation turned live location on").toBe(false);
        expect((await shareeApi.get(`safety/checkins/${checkin.slug}/`)).status()).toBe(404);
        expect((await shareeApi.get(`safety/checkins/${checkin.slug}/location/`)).status()).toBe(404);
    });
});
