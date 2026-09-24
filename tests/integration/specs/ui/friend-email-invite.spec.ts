/**
 * Friend invitations by email, and tagging a visit participant by email.
 *
 * The sender must see the same thing whether the address belongs to an account or not, and the invitee
 * answers the friendship on its own page. The unregistered path past the email is covered in pytest.
 */

import { randomUUID } from "node:crypto";

import type { Locator } from "@playwright/test";

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";
import { requireAccount, SHAREE_ROLE } from "../../lib/accounts.js";
import { resourceName } from "../../lib/env.js";
import { ensureFriends, whoami } from "../../lib/friendship.js";
import { waitForOrNull } from "../../lib/waiting.js";

interface Notification {
    title?: string;
    message?: string;
    url?: string;
}

const DELIVERY_TIMEOUT_MS = 60_000;

function uniqueName(label: string): string {
    return resourceName(`${label} ${randomUUID().slice(0, 8)}`);
}

/** An address no account has, on the reserved domain the site never hands to its mail relay. */
function unregisteredAddress(): string {
    return `${uniqueName("nobody").toLowerCase().replace(/[^a-z0-9]+/g, "-")}@e2e.invalid`;
}

async function listOf<T>(api: ApiClient, path: string): Promise<T[]> {
    const body = await api.json<T[] | { results?: T[] }>("get", path);
    return Array.isArray(body) ? body : (body.results ?? []);
}

async function areFriends(api: ApiClient, otherUuid: string): Promise<boolean> {
    const rows = await listOf<unknown>(api, "friends/?status=Accepted");
    return rows.some((row) => JSON.stringify(row).includes(otherUuid));
}

async function relatedAtAll(api: ApiClient, otherUuid: string): Promise<boolean> {
    for (const status of ["Pending", "Requested", "Accepted"]) {
        if ((await listOf<unknown>(api, `friends/?status=${status}`)).some((row) => JSON.stringify(row).includes(otherUuid))) {
            return true;
        }
    }
    return false;
}

async function endFriendship(api: ApiClient, otherUuid: string): Promise<void> {
    const response = await api.delete(`friends/${otherUuid}/`);
    expect([200, 204, 404], `removing the friendship answered ${response.status()}`).toContain(response.status());
}

async function waitForNotification(api: ApiClient, what: string, matches: (row: Notification) => boolean): Promise<Notification | null> {
    return waitForOrNull(
        async () => (await listOf<Notification>(api, "notifications/")).find(matches) ?? null,
        (row) => row !== null,
        { what, timeoutMs: DELIVERY_TIMEOUT_MS, intervalMs: 2_000, describe: (row) => JSON.stringify(row) },
    );
}

async function shapeWithout(row: Locator, names: string[]): Promise<string> {
    let html = await row.evaluate((element) => element.outerHTML);
    for (const name of names) {
        html = html.replaceAll(name, "NAME");
    }
    return html;
}

test.describe("inviting a friend by email", () => {
    ifSharingPair()("an account is asked on the invitation page, and nothing exists until it accepts", async ({ api, shareeApi, shareePage }) => {
        const sharee = requireAccount(SHAREE_ROLE);
        const [inviter, invitee] = [await whoami(api), await whoami(shareeApi)];
        const wereFriends = await areFriends(api, invitee.uuid);
        await endFriendship(api, invitee.uuid);
        try {
            const sent = await api.post("friend-invites/", { email: sharee.email });
            expect(sent.status(), `inviting by email answered ${sent.status()}`).toBe(200);
            expect(await sent.json()).toEqual({ result: "sent" });

            // Newest first; a re-invite while one is still open reuses it rather than asking again.
            const notification = await waitForNotification(shareeApi, "the invitee's friend invitation", (row) => (row.url ?? "").includes("/friendship/invitations/"));
            expect(notification, "no friend invitation reached the invitee; is a Celery worker running?").not.toBeNull();
            expect(await relatedAtAll(api, invitee.uuid), "a friendship row exists before the invitee answered").toBe(false);

            await shareePage.goto(notification!.url!);
            await expect(shareePage.getByRole("heading", { name: /wants to be friends/ })).toBeVisible();
            await shareePage.getByRole("button", { name: "Accept" }).click();

            await expect.poll(() => areFriends(api, invitee.uuid), { message: "accepting did not make them friends" }).toBe(true);
            expect(await areFriends(shareeApi, inviter.uuid)).toBe(true);
        } finally {
            if (!wereFriends) {
                await endFriendship(api, invitee.uuid);
            }
        }
    });

    ifSharingPair()("tagging a visit participant by email looks the same for an account as for a stranger", async ({ page, api, shareeApi }) => {
        const sharee = requireAccount(SHAREE_ROLE);
        const invitee = await whoami(shareeApi);
        const wereFriends = await areFriends(api, invitee.uuid);
        await endFriendship(api, invitee.uuid);
        const pinName = uniqueName("tagged visit");
        const pin = await api.createPin({ name: pinName });
        const earlier = new Set((await listOf<Notification>(shareeApi, "notifications/")).map((row) => JSON.stringify(row)));

        try {
            await page.goto(`/dashboard/map/pin/${pin.slug}/`);
            await page.locator('[data-tab="visits"]').click();
            await page.locator("#visit-history-panel").getByTitle("Log a visit").click();
            const dialog = page.locator(`#visit-add-dialog-${pin.slug}`);
            await expect(dialog).toBeVisible();
            await dialog.locator(".visit-attachment-toggle", { hasText: "Participants" }).click();
            const people: [string, string][] = [
                ["Tagged Member", sharee.email],
                ["Tagged Stranger", unregisteredAddress()],
            ];
            for (const [name, email] of people) {
                await dialog.getByRole("button", { name: /Add someone not on UrbanLens/ }).click();
                const row = dialog.locator(".visit-external-row").last();
                await row.getByPlaceholder("Name").fill(name);
                await row.getByPlaceholder("Email (optional)").fill(email);
                await expect(row.getByRole("checkbox")).toBeChecked();
            }
            await dialog.getByRole("button", { name: "Save Visit" }).click();

            const names = page.locator("#visit-history-panel .visit-external-name");
            await expect(names).toHaveCount(2);
            const labels = people.map(([name]) => name);
            expect(await shapeWithout(names.nth(0), labels), "an address with an account is shown differently").toBe(await shapeWithout(names.nth(1), labels));
            await expect(page.locator("#visit-history-panel")).not.toContainText(sharee.username);

            const suggestion = await waitForNotification(shareeApi, "the tagged member's visit suggestion", (row) => !earlier.has(JSON.stringify(row)) && /suggested you also visited/.test(row.message ?? ""));
            expect(suggestion, "the tagged account was not offered the visit").not.toBeNull();
            await page.reload();
            await page.locator('[data-tab="visits"]').click();
            await expect(page.locator("#visit-history-panel .visit-external-name")).toHaveCount(2);
            await expect(page.locator("#visit-history-panel")).not.toContainText(sharee.username);
            expect(await relatedAtAll(api, invitee.uuid), "tagging made a friendship row before the member answered").toBe(false);
        } finally {
            await api.delete(`pins/${pin.slug}/`);
            if (wereFriends) {
                await ensureFriends(api, shareeApi);
            }
        }
    });
});
