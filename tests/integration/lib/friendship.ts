/**
 * Makes two accounts friends, idempotently. Consent-copy flows (pin shares, member contacts,
 * direct messages) all require an accepted friendship, and the `sharer`/`sharee` pair keeps theirs
 * for the life of the deployment rather than resetting it per test.
 */

import { expect } from "@playwright/test";

import type { ApiClient } from "./api-client.js";

interface FriendEntry {
    status?: string;
    profile?: { uuid?: string };
    friend?: { uuid?: string };
    other?: { uuid?: string };
}

export async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string; username?: string }> {
    return api.json<{ uuid: string; slug: string; username?: string }>("get", "whoami/");
}

async function relationship(api: ApiClient, otherUuid: string): Promise<FriendEntry | null> {
    for (const status of [undefined, "Pending", "Requested", "Accepted"]) {
        const feed = await api.json<{ results?: FriendEntry[] } | FriendEntry[]>("get", "friends/", status ? { status } : undefined);
        const rows = Array.isArray(feed) ? feed : (feed.results ?? []);
        const match = rows.find((row) => JSON.stringify(row).includes(otherUuid));
        if (match) {
            return match;
        }
    }
    return null;
}

function isAccepted(entry: FriendEntry | null): boolean {
    return String(entry?.status ?? "").toLowerCase().includes("accept");
}

/** Ensure `a` and `b` are accepted friends; returns both identities. */
export async function ensureFriends(a: ApiClient, b: ApiClient): Promise<{ a: { uuid: string; slug: string }; b: { uuid: string; slug: string } }> {
    const [me, them] = [await whoami(a), await whoami(b)];
    if (!isAccepted(await relationship(a, them.uuid))) {
        const requested = await a.post("friends/", { profile_uuid: them.uuid });
        // 400 means a request already exists in some direction; accepting settles either case.
        expect(requested.status(), `friend request answered ${requested.status()}: ${(await requested.text()).slice(0, 200)}`).toBeLessThan(500);
        if (!isAccepted(await relationship(b, me.uuid))) {
            await b.post(`friends/${me.uuid}/accept/`);
        }
        if (!isAccepted(await relationship(a, them.uuid))) {
            await a.post(`friends/${them.uuid}/accept/`);
        }
    }
    expect(isAccepted(await relationship(a, them.uuid)), "the sharing pair could not be made friends").toBe(true);
    return { a: me, b: them };
}
