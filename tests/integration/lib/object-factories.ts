/**
 * Throwaway objects one account owns, for specs that then probe them as another account. Every
 * factory registers its own cleanup on the client that created it.
 */

import { expect, type APIRequestContext } from "@playwright/test";

import { ApiError, type ApiClient } from "./api-client.js";
import { apiUrl, resourceName } from "./env.js";
import { uniqueMarker } from "./security.js";

/** An environment reason a factory could not build its object, as opposed to a product failure. */
export interface Unavailable {
    unavailable: string;
}

export function isUnavailable(value: unknown): value is Unavailable {
    return typeof value === "object" && value !== null && "unavailable" in value;
}

/** {@link uniqueMarker} plus a random tail, so two tests (or `--repeat-each` runs) never share one. */
export function randomMarker(label = ""): string {
    return `${uniqueMarker(label)}${crypto.randomUUID().replace(/-/g, "").slice(0, 8)}`;
}

/** A page's rows, whether the endpoint pages (`{results}`) or answers a bare array. */
export function rowsOf<T = Record<string, unknown>>(body: unknown): T[] {
    if (Array.isArray(body)) {
        return body as T[];
    }
    if (body && typeof body === "object" && Array.isArray((body as { results?: unknown }).results)) {
        return (body as { results: T[] }).results;
    }
    return [];
}

/** The identifier an item route takes, whichever key the payload uses for it. */
export function idOf(row: unknown): string {
    const record = (row ?? {}) as Record<string, unknown>;
    for (const key of ["id", "uuid", "slug", "comment_id", "note_id", "visit_id", "revision_id"]) {
        const value = record[key];
        if (typeof value === "string" || typeof value === "number") {
            return String(value);
        }
    }
    throw new Error(`no identifier in ${JSON.stringify(row).slice(0, 200)}`);
}

/** POSTs `body` to `path` as `api`, raising on a non-2xx, and returns the created row's identifier. */
export async function createChild(api: ApiClient, path: string, body: unknown): Promise<string> {
    return idOf(await api.json("post", path, body));
}

function suffix(): string {
    return crypto.randomUUID().slice(0, 8);
}

export async function createList(api: ApiClient, name = resourceName(`list ${suffix()}`)): Promise<{ slug: string; name: string }> {
    const created = await api.json<{ slug: string; name: string }>("post", "lists/", {
        name,
        description: "Owned by the security suite; not for sharing.",
    });
    expect(created.slug, `list create carried no slug: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("list", created.slug, () => api.delete(`lists/${created.slug}/`));
    return created;
}

export async function createTrip(api: ApiClient, name = resourceName(`trip ${suffix()}`)): Promise<{ slug: string; name: string }> {
    const created = await api.json<{ slug: string; name: string }>("post", "trips/", {
        name,
        description: "Owned by the security suite; not for sharing.",
    });
    expect(created.slug, `trip create carried no slug: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("trip", created.slug, () => api.delete(`trips/${created.slug}/`));
    return created;
}

/** A tag label. Label names are unique per profile, hence the random tail on the default. */
export async function createLabel(api: ApiClient, name = resourceName(`label ${suffix()}`)): Promise<{ uuid: string; name: string }> {
    const created = await api.json<{ uuid: string; name: string }>("post", "labels/", { name, kind: "tag" });
    expect(created.uuid, `label create carried no uuid: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("label", created.uuid, () => api.delete(`labels/${created.uuid}/`));
    return created;
}

export async function createSavedFilter(api: ApiClient, name = resourceName(`filter ${suffix()}`)): Promise<{ uuid: string; name: string }> {
    const created = await api.json<{ uuid: string; name: string }>("post", "saved-filters/", {
        name,
        criteria: { security: { max: 1 } },
    });
    expect(created.uuid, `saved filter create carried no uuid: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("saved-filter", created.uuid, () => api.delete(`saved-filters/${created.uuid}/`));
    return created;
}

export async function createCustomField(
    api: ApiClient,
    entityType: "pin" | "photo" | "profile" | "markup_map" = "pin",
    name = resourceName(`field ${suffix()}`),
): Promise<{ id: string; name: string }> {
    const created = await api.json<Record<string, unknown>>("post", "custom-fields/", { name, entity_type: entityType, field_type: "text" });
    const id = idOf(created);
    api.track("custom-field", id, () => api.delete(`custom-fields/${id}/`));
    return { id, name: String(created.name ?? name) };
}

/** A 1x1 PNG, with `marker` appended after IEND so the upload is traceable in storage. */
function tinyPng(marker: string): Buffer {
    return Buffer.concat([
        Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64"),
        Buffer.from(`\n${marker}`, "utf-8"),
    ]);
}

/**
 * Uploads a private photo onto `pinSlug` as `api`.
 *
 * @returns The photo, or {@link Unavailable} when the malware scanner is down (503) - the upload path
 *     refuses rather than storing unscanned bytes, which is correct and not what the caller is testing.
 */
export async function uploadPhoto(request: APIRequestContext, api: ApiClient, pinSlug: string, caption: string): Promise<{ uuid: string } | Unavailable> {
    if (!api.apiKey) {
        return { unavailable: "the uploading account has no API key" };
    }
    const upload = await request.post(apiUrl("photos/"), {
        headers: { Authorization: `Bearer ${api.apiKey}` },
        multipart: {
            file: { name: `${suffix()}.png`, mimeType: "image/png", buffer: tinyPng(caption) },
            caption,
            pin: pinSlug,
        },
    });
    if (upload.status() === 503) {
        return { unavailable: `photo upload answered 503 (malware scanner unavailable): ${(await upload.text()).slice(0, 160)}` };
    }
    if (!upload.ok()) {
        throw new ApiError("POST", "photos/", upload.status(), await upload.text());
    }
    const photo = (await upload.json()) as { uuid: string };
    api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));
    return photo;
}

/**
 * Opens a check-in and, unless `keepOpen`, cancels it straight away.
 *
 * A cancelled check-in stays readable by its owner, and cancelling frees the one-active-per-scope slot
 * other specs in the run need.
 *
 * @returns The check-in, or {@link Unavailable} when the account already has an active one (409),
 *     which means another spec holds the slot right now.
 */
export async function openCheckin(api: ApiClient, title: string, keepOpen = false): Promise<{ slug: string; uuid: string } | Unavailable> {
    const created = await api.post("safety/checkins/", {
        title,
        checkin_by: new Date(Date.now() + 120 * 60_000).toISOString(),
    });
    if (created.status() === 409) {
        return { unavailable: "the account already has an active check-in, most likely another spec's" };
    }
    if (!created.ok()) {
        throw new ApiError("POST", "safety/checkins/", created.status(), await created.text());
    }
    const checkin = (await created.json()) as { slug: string; uuid: string };
    api.track("check-in", checkin.slug, () => api.delete(`safety/checkins/${checkin.slug}/`));
    if (!keepOpen) {
        const cancelled = await api.post(`safety/checkins/${checkin.slug}/cancel/`);
        expect(cancelled.ok(), `cancelling a fresh check-in answered ${cancelled.status()}: ${(await cancelled.text()).slice(0, 200)}`).toBeTruthy();
    }
    return checkin;
}

/** Midday UTC, `daysAgo` days back - far enough from midnight that a server timezone cannot move the date. */
export function middayUtc(daysAgo: number, yearsAgo = 0): string {
    const now = new Date();
    const day = new Date(Date.UTC(now.getUTCFullYear() - yearsAgo, now.getUTCMonth(), now.getUTCDate() - daysAgo, 12));
    return day.toISOString();
}

export async function logVisit(api: ApiClient, pinSlug: string, visitedAt: string, notes: string): Promise<string> {
    return createChild(api, `pins/${pinSlug}/visits/`, { visited_at: visitedAt, notes });
}

/** `"south,west,north,east"` around a point, `pad` degrees each way. */
export function boundsAround(latitude: number, longitude: number, pad = 0.001): string {
    return [latitude - pad, longitude - pad, latitude + pad, longitude + pad].join(",");
}
