/**
 * Lists backed by a saved filter: automatic membership follows the filter, manual add/remove
 * decisions persist against it, and no pin is listed twice (GOALS "Discovery: browse and search").
 *
 * `lists/{slug}/resync/` is capped at 12 an hour per key, so each test resyncs at most once and
 * leans on the pin-save signal otherwise.
 */

import type { APIRequestContext, APIResponse } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl, resourceName } from "../../lib/env.js";
import type { ApiClient, CreatedPin } from "../../lib/api-client.js";

interface Member {
    added_via: string;
    pin: { uuid: string; name: string };
}

interface SmartListFixture {
    slug: string;
    member: CreatedPin;
    outsider: CreatedPin;
    marker: string;
}

const GOALS_RECONCILE =
    'GOALS: "manual decisions persist even as filter membership changes, and a pin present via both the filter and a manual add is never shown twice"';

function marker(): string {
    return `sl${Math.random().toString(36).slice(2, 10)}`;
}

/** A saved filter matching `marker`, a smart list built from it, one pin it matches and one it does not. */
async function smartList(api: ApiClient): Promise<SmartListFixture> {
    const token = marker();
    const member = await api.createPin({ name: resourceName(`${token} member`) });
    const outsider = await api.createPin({ name: resourceName("outsider") });
    // Priority is what "stops matching" changes: a renamed pin keeps its old name as an alias, and the
    // name criterion matches aliases, so a rename never takes a pin out of a name filter.
    const prioritised = await api.patch(`pins/${member.slug}/`, { priority: 5 });
    expect(prioritised.status(), `setting the member's priority answered ${prioritised.status()}`).toBe(200);
    expect((await prioritised.json()).priority, "the priority PATCH was accepted but not applied").toBe(5);

    const filter = await api.json<{ uuid: string }>("post", "saved-filters/", { name: resourceName(`${token} filter`), criteria: { name: token, min_priority: 4 } });
    api.track("saved-filter", filter.uuid, () => api.delete(`saved-filters/${filter.uuid}/`));

    // Creation resyncs a list that has rules, so no resync call is spent here.
    const list = await api.json<{ slug: string; is_smart?: boolean }>("post", "lists/", {
        name: resourceName(`${token} smart list`),
        is_smart: true,
        source_saved_filter_uuid: filter.uuid,
    });
    api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));
    return { slug: list.slug, member, outsider, marker: token };
}

async function members(api: ApiClient, slug: string): Promise<Member[]> {
    const page = await api.json<{ results: Member[] }>("get", `lists/${slug}/items/`, { page_size: "100" });
    return page.results;
}

function occurrences(rows: Member[], pin: CreatedPin): number {
    return rows.filter((row) => row.pin.uuid === pin.uuid).length;
}

/** `ApiClient.delete` sends no body, and this endpoint takes its uuids in one. */
async function removeFromList(request: APIRequestContext, api: ApiClient, slug: string, pins: CreatedPin[]): Promise<APIResponse> {
    return request.delete(apiUrl(`lists/${slug}/items/`), {
        headers: { Authorization: `Bearer ${api.apiKey}` },
        data: { pin_uuids: pins.map((pin) => pin.uuid) },
    });
}

async function resync(api: ApiClient, slug: string): Promise<void> {
    const response = await api.post(`lists/${slug}/resync/`);
    expect(response.status(), `resync answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
}

function goalsConflict(code: string): void {
    test.info().annotations.push({ type: "goals-conflict", description: `${GOALS_RECONCILE} vs ${code}` });
}

test.describe("smart lists", () => {
    test("filter membership is reconciled with manual adds", async ({ api }) => {
        const list = await smartList(api);

        const before = await members(api, list.slug);
        expect(occurrences(before, list.member), "a pin the filter matches is not on the list").toBe(1);
        expect(occurrences(before, list.outsider), "a pin the filter does not match is on the list").toBe(0);
        expect(before.find((row) => row.pin.uuid === list.member.uuid)?.added_via).toBe("smart_filter");

        const added = await api.json<{ added: number }>("post", `lists/${list.slug}/items/`, { pin_uuids: [list.outsider.uuid] });
        expect(added.added, "a manual add of a non-matching pin was not counted").toBe(1);

        const after = await members(api, list.slug);
        expect(occurrences(after, list.member)).toBe(1);
        expect(occurrences(after, list.outsider)).toBe(1);
        expect(after.find((row) => row.pin.uuid === list.outsider.uuid)?.added_via).toBe("manual");
    });

    test("a pin matched by both the filter and a manual add appears once", async ({ api }) => {
        const list = await smartList(api);

        const added = await api.json<{ added: number }>("post", `lists/${list.slug}/items/`, { pin_uuids: [list.member.uuid, list.member.uuid] });
        expect(added.added, "a pin already on the list via the filter was added a second time").toBe(0);

        const rows = await members(api, list.slug);
        expect(occurrences(rows, list.member), "a pin present via the filter and a manual add is listed more than once").toBe(1);
        expect(occurrences(rows, list.outsider)).toBe(0);
    });

    test("removing a filter member takes it off the list", async ({ api, apiRequestContext }) => {
        const list = await smartList(api);
        expect(occurrences(await members(api, list.slug), list.member)).toBe(1);

        const removed = await removeFromList(apiRequestContext, api, list.slug, [list.member]);
        expect(removed.status(), `removing answered ${removed.status()}: ${(await removed.text()).slice(0, 200)}`).toBe(200);
        expect(((await removed.json()) as { removed: number }).removed).toBe(1);
        expect(occurrences(await members(api, list.slug), list.member)).toBe(0);

        const again = await removeFromList(apiRequestContext, api, list.slug, [list.outsider]);
        expect(((await again.json()) as { removed: number }).removed, "removing a pin that was never on the list reported a removal").toBe(0);
    });

    test("a manual removal survives a resync and an edit of the pin", async ({ api, apiRequestContext }) => {
        test.fail();
        goalsConflict("services/pins/pin_list_membership.py:267 deletes the row, so resync_smart_list (:90) and sync_pin_against_smart_lists (:79) re-add it");
        const list = await smartList(api);

        const removed = await removeFromList(apiRequestContext, api, list.slug, [list.member]);
        expect(removed.status()).toBe(200);

        await resync(api, list.slug);
        expect(occurrences(await members(api, list.slug), list.member), "a manually removed pin came back on resync").toBe(0);

        const edited = await api.patch(`pins/${list.member.slug}/`, { description: `edited ${list.marker}` });
        expect(edited.status()).toBe(200);
        expect(occurrences(await members(api, list.slug), list.member), "a manually removed pin came back when the pin was saved").toBe(0);
        expect(occurrences(await members(api, list.slug), list.outsider)).toBe(0);
    });

    test("a pin that stops matching the filter leaves the list", async ({ api }) => {
        const list = await smartList(api);
        expect(occurrences(await members(api, list.slug), list.member)).toBe(1);

        const lowered = await api.patch(`pins/${list.member.slug}/`, { priority: 1 });
        expect(lowered.status(), `lowering the priority answered ${lowered.status()}: ${(await lowered.text()).slice(0, 200)}`).toBe(200);

        // The save signal can hand the sync to the queue when the account has many smart lists.
        await expect.poll(async () => occurrences(await members(api, list.slug), list.member), { timeout: 60_000 }).toBe(0);
        expect(occurrences(await members(api, list.slug), list.outsider)).toBe(0);
    });

    test("a manual add of a filter member survives the filter no longer matching", async ({ api }) => {
        test.fail();
        goalsConflict("services/pins/pin_list_membership.py:234 skips a pin already present, so the manual decision is never recorded and :86 removes it");
        const list = await smartList(api);
        expect(occurrences(await members(api, list.slug), list.member)).toBe(1);

        const added = await api.post(`lists/${list.slug}/items/`, { pin_uuids: [list.member.uuid] });
        expect(added.status()).toBe(200);

        const lowered = await api.patch(`pins/${list.member.slug}/`, { priority: 1 });
        expect(lowered.status()).toBe(200);
        await resync(api, list.slug);

        const rows = await members(api, list.slug);
        expect(occurrences(rows, list.member), "a pin the user added by hand was dropped when the filter stopped matching it").toBe(1);
        expect(occurrences(rows, list.outsider)).toBe(0);
    });
});
