/**
 * Inviting someone to a trip by email address.
 *
 * The inviter must see the same thing whether the address belongs to an account or not, and the invitee
 * must answer "join the trip?" and "become friends?" separately. The unregistered path past the email
 * (the link, declining without an account, signing up) is covered in pytest, since no inbox is reachable here.
 */

import { randomUUID } from "node:crypto";

import type { Locator } from "@playwright/test";

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";
import { requireAccount, SHAREE_ROLE } from "../../lib/accounts.js";
import { resourceName } from "../../lib/env.js";
import { whoami } from "../../lib/friendship.js";
import { appRoutes } from "../../lib/routes.js";
import { waitForOrNull } from "../../lib/waiting.js";

interface Trip {
    slug: string;
    name: string;
}

interface Notification {
    message?: string;
    url?: string;
}

interface Member {
    profile?: { uuid?: string };
}

const DELIVERY_TIMEOUT_MS = 60_000;

/** A resource name no other test or repeat in this run shares. */
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

async function endFriendship(api: ApiClient, otherUuid: string): Promise<void> {
    const response = await api.delete(`friends/${otherUuid}/`);
    expect([200, 204, 404], `removing the friendship answered ${response.status()}`).toContain(response.status());
}

async function areFriends(api: ApiClient, otherUuid: string): Promise<boolean> {
    const rows = await listOf<{ status?: string }>(api, "friends/?status=Accepted");
    return rows.some((row) => JSON.stringify(row).includes(otherUuid));
}

/** A pending row with its address blanked, so two rows can be compared as markup. */
async function rowShape(row: Locator, address: string): Promise<string> {
    const html = await row.evaluate((element) => element.outerHTML);
    return html
        .replaceAll(address, "ADDRESS")
        .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, "UUID");
}

test.describe("inviting someone to a trip by email", () => {
    ifSharingPair()("the inviter sees an account's address exactly as they see an unregistered one", async ({ page, api, shareeApi }) => {
        const sharee = requireAccount(SHAREE_ROLE);
        const shareeProfile = await whoami(shareeApi);
        const stranger = unregisteredAddress();
        const name = uniqueName("email invite trip");

        await page.goto(appRoutes.trips);
        await page.locator("[onclick*='create-trip-dialog'], [data-ul-open-dialog='create-trip-dialog']").first().click();
        const dialog = page.locator("#create-trip-dialog");
        await expect(dialog).toBeVisible();
        await dialog.locator("#trip-name").fill(name);
        await dialog.locator("#trip-invite-emails").fill(`${sharee.email}, ${stranger}`);
        await dialog.getByRole("button", { name: "Create Trip" }).click();
        await expect(dialog).toBeHidden();

        const trip = (await listOf<Trip>(api, "trips/")).find((row) => row.name === name);
        expect(trip, `the trip "${name}" was not created`).toBeTruthy();
        api.track("trip", trip!.slug, () => api.delete(`trips/${trip!.slug}/`));

        await page.goto(`/dashboard/trips/${trip!.slug}/`);
        const panel = page.locator("#trip-members-panel");
        const rows = panel.locator("[data-trip-invitation]");
        await expect(rows).toHaveCount(2);
        const accountRow = rows.filter({ hasText: sharee.email });
        const strangerRow = rows.filter({ hasText: stranger });
        await expect(accountRow).toHaveCount(1);
        await expect(strangerRow).toHaveCount(1);
        expect(await rowShape(accountRow, sharee.email), "the row for an address with an account differs from one without").toBe(
            await rowShape(strangerRow, stranger),
        );
        await expect(panel.locator(".trip-member-username"), "the invitee's account was named on the roster before they answered").not.toContainText([`@${sharee.username}`]);
        const members = await listOf<Member>(api, `trips/${trip!.slug}/members/`);
        expect(members.some((member) => member.profile?.uuid === shareeProfile.uuid), "the invitee was put on the roster before answering").toBe(false);

        const listed = await listOf<{ email?: string }>(api, `trips/${trip!.slug}/invitations/`);
        expect(listed.map((row) => row.email).sort()).toEqual([sharee.email, stranger].sort());

        page.once("dialog", (confirm) => void confirm.accept());
        await strangerRow.getByTitle("Withdraw invitation").click();
        await expect(panel.locator("[data-trip-invitation]")).toHaveCount(1);
        await expect(panel).not.toContainText(stranger);
    });

    ifSharingPair()("the invitee joins the trip and declines the friendship, as separate answers", async ({ api, shareeApi, shareePage }) => {
        const sharee = requireAccount(SHAREE_ROLE);
        const [inviter, invitee] = [await whoami(api), await whoami(shareeApi)];
        await endFriendship(api, invitee.uuid);

        const trip = await api.json<Trip>("post", "trips/", { name: uniqueName("invitee answers trip") });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));
        const invited = await api.post(`trips/${trip.slug}/invitations/`, { email: sharee.email });
        expect(invited.status(), `inviting by email answered ${invited.status()}: ${(await invited.text()).slice(0, 200)}`).toBe(202);

        const notification = await waitForOrNull(
            async () => (await listOf<Notification>(shareeApi, "notifications/")).find((row) => (row.url ?? "").includes("/trips/invitations/") && (row.message ?? "").includes(trip.name)) ?? null,
            (row) => row !== null,
            { what: "the invitee's trip invitation notification", timeoutMs: DELIVERY_TIMEOUT_MS, intervalMs: 2_000, describe: (row) => JSON.stringify(row) },
        );
        expect(notification, "no trip invitation notification reached the invitee; is a Celery worker running?").not.toBeNull();

        await shareePage.goto(notification!.url!);
        await expect(shareePage.getByRole("heading", { name: "Join the trip?" })).toBeVisible();
        const friendQuestion = shareePage.locator("section", { has: shareePage.getByRole("heading", { name: /Become friends with/ }) });
        await expect(friendQuestion).toBeVisible();

        await friendQuestion.getByRole("button", { name: "Decline" }).click();
        await expect(shareePage.getByRole("heading", { name: /Become friends with/ })).toHaveCount(0);
        await expect(shareePage.getByRole("heading", { name: "Join the trip?" })).toBeVisible();

        await shareePage.getByRole("button", { name: "Join trip" }).click();
        await expect(shareePage).toHaveURL(new RegExp(`/dashboard/trips/${trip.slug}/`));

        const members = await listOf<Member>(api, `trips/${trip.slug}/members/`);
        expect(members.some((member) => member.profile?.uuid === invitee.uuid), "joining the trip did not put the invitee on the roster").toBe(true);
        expect(await areFriends(api, invitee.uuid), "declining the friend question still made them friends").toBe(false);
        expect(await areFriends(shareeApi, inviter.uuid)).toBe(false);
        expect(await listOf(api, `trips/${trip.slug}/invitations/`), "an answered invitation is still listed as open").toEqual([]);
    });
});
