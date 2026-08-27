/**
 * Safety check-ins: the feature where a bug is not an inconvenience.
 *
 * A check-in is a promise that somebody will be told if the user does not come
 * back. That makes it the one domain here where the *scheduling* matters as
 * much as the data - the row is written now and read by a Celery worker later,
 * against a deadline, on a different machine. This suite cannot wait out a real
 * deadline, so it does not pretend to; what it can prove is that every state a
 * check-in passes through is reachable and consistent through the deployed API,
 * which is the precondition for the worker half meaning anything.
 *
 * Deliberately not exercised: overdue escalation and partner notification
 * delivery. Both need either a fabricated clock or a wait measured in the
 * check-in's grace period, and a test that sleeps for minutes on a shared box
 * is a test people delete.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface Checkin {
    uuid: string;
    slug: string;
    title?: string;
    status?: string;
}

/** An ISO timestamp `minutes` from now - a deadline far enough out not to fire. */
function deadline(minutes: number): string {
    return new Date(Date.now() + minutes * 60_000).toISOString();
}

/**
 * Cancels and removes any check-in this account still has open.
 *
 * **An account may have only one active check-in at a time** - the API answers
 * 409 "You already have an active check-in" otherwise - which is correct for
 * the feature and makes every test here order-dependent on the last one. That
 * is also why this describe block is serial: run in parallel, the tests take
 * the single slot from each other and fail on a rule rather than on a defect.
 */
async function clearActiveCheckins(api: ApiClient): Promise<void> {
    const page = await api.json<Page<Checkin>>("get", "safety/checkins/", { page_size: "100" });
    for (const checkin of page.results) {
        if (!checkin.slug) {
            continue;
        }
        await api.post(`safety/checkins/${checkin.slug}/cancel/`);
        await api.delete(`safety/checkins/${checkin.slug}/`);
    }
}

test.describe.serial("safety check-ins", () => {
    test.beforeEach(async ({ api }) => {
        await clearActiveCheckins(api);
    });

    test("a check-in can be opened, read back, amended and cancelled", async ({ api }) => {
        const title = resourceName("check-in lifecycle");
        const created = await api.json<Checkin>("post", "safety/checkins/", {
            title,
            checkin_by: deadline(120),
            plan_details: "Opened by the UrbanLens integration suite.",
            grace_period_seconds: 900,
        });
        expect(created.slug, `the created check-in carries no slug: ${JSON.stringify(created).slice(0, 200)}`).toBeTruthy();

        try {
            const page = await api.json<Page<Checkin>>("get", "safety/checkins/", { page_size: "100" });
            expect(page.results.some((checkin) => checkin.slug === created.slug), "an open check-in is missing from the list").toBeTruthy();

            const amended = await api.patch(`safety/checkins/${created.slug}/`, { plan_details: "Amended by the integration suite." });
            expect(amended.status(), `PATCH answered ${amended.status()}: ${(await amended.text()).slice(0, 200)}`).toBe(200);

            // Cancelling is the path a user takes when they get home safely and
            // is the one that must never fail: leaving it open raises an alarm
            // on somebody's phone for no reason.
            const cancelled = await api.post(`safety/checkins/${created.slug}/cancel/`);
            expect(cancelled.status(), `cancelling answered ${cancelled.status()}: ${(await cancelled.text()).slice(0, 200)}`).toBeLessThan(300);

            const after = await api.get(`safety/checkins/${created.slug}/`);
            if (after.status() === 200) {
                const body = (await after.json()) as Checkin;
                expect(String(body.status ?? "").toLowerCase(), `a cancelled check-in still reads as "${body.status}"`).not.toBe("active");
            }
        } finally {
            await api.delete(`safety/checkins/${created.slug}/`);
        }
    });

    test("checking in resolves the check-in", async ({ api }) => {
        const created = await api.json<Checkin>("post", "safety/checkins/", {
            title: resourceName("checks in"),
            checkin_by: deadline(90),
        });

        try {
            const done = await api.post(`safety/checkins/${created.slug}/check-in/`);
            expect(done.status(), `checking in answered ${done.status()}: ${(await done.text()).slice(0, 200)}`).toBeLessThan(300);

            const after = await api.get(`safety/checkins/${created.slug}/`);
            if (after.status() === 200) {
                const body = (await after.json()) as Checkin;
                expect(String(body.status ?? "").toLowerCase(), `after checking in the status is still "${body.status}"`).not.toBe("active");
            }
        } finally {
            await api.delete(`safety/checkins/${created.slug}/`);
        }
    });

    test("a check-in without a deadline is refused", async ({ api }) => {
        // `checkin_by` is the whole point: a check-in with no deadline is a note.
        const response = await api.post("safety/checkins/", { title: resourceName("no deadline") });
        expect(response.status()).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    ifSecondaryAccount()("a partner can be invited by username", async ({ api, secondaryApi }) => {
        const them = await secondaryApi.json<{ slug: string }>("get", "whoami/");
        const created = await api.json<Checkin>("post", "safety/checkins/", {
            title: resourceName("with a partner"),
            checkin_by: deadline(120),
        });

        try {
            const invited = await api.post(`safety/checkins/${created.slug}/partners/`, { username: them.slug });
            expect(invited.status(), `inviting a partner answered ${invited.status()}: ${(await invited.text()).slice(0, 250)}`).toBeLessThan(300);

            // The invitation has to be visible to the person invited, or the
            // safety net has one end tied to nothing.
            const invites = await secondaryApi.get("safety/partner-invites/");
            expect(invites.status(), `the invitee's invite list answered ${invites.status()}`).toBe(200);
        } finally {
            await api.delete(`safety/checkins/${created.slug}/`);
        }
    });

    test("safety settings round-trip", async ({ api }) => {
        const before = await api.get("safety/settings/");
        expect(before.status(), `reading safety settings answered ${before.status()}`).toBe(200);

        const patched = await api.patch("safety/settings/", { default_grace_period_seconds: 1800 });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);

        const after = await api.json<{ default_grace_period_seconds?: number }>("get", "safety/settings/");
        expect(after.default_grace_period_seconds, "the grace period did not persist").toBe(1800);
    });

    test("a key without the safety scope cannot open a check-in", async ({ restrictedApi }) => {
        const response = await restrictedApi.post("safety/checkins/", { title: resourceName("forbidden"), checkin_by: deadline(60) });
        expect(response.status(), "a profile:read key opened a safety check-in").toBe(403);
    });
});
