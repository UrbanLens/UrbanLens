/**
 * Session-side helpers for consent-copy flows that have no external-API route: sending a pin share
 * (`/dashboard/map/pin/<slug>/share/send/`), reading the recipient's share pages, and posting to
 * other CSRF-protected web forms such as `/dashboard/messages/<slug>/send/`.
 */

import { expect, type APIResponse, type Page } from "@playwright/test";

import type { ApiClient, CreatedPin } from "./api-client.js";
import { env } from "./env.js";
import { uniqueMarker } from "./security.js";

/** {@link uniqueMarker} plus a per-call random suffix, so two markers in one run never collide. */
export function freshMarker(label: string): string {
    return `${uniqueMarker(label)}${Math.random().toString(36).slice(2, 8)}`;
}

/** The session's CSRF token, fetching `warmPath` first when the context has no cookie yet. */
export async function csrfToken(page: Page, warmPath = "/dashboard/"): Promise<string> {
    const read = async () => (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
    let token = await read();
    if (!token) {
        await page.request.get(warmPath);
        token = await read();
    }
    expect(token, `no csrftoken cookie after GET ${warmPath}, so a web form POST would fail on CSRF rather than on the rule under test`).toBeTruthy();
    return token ?? "";
}

/** POSTs a urlencoded form as the page's signed-in user, with CSRF and same-origin headers. */
export async function sessionPost(page: Page, path: string, form: Record<string, string>, warmPath?: string): Promise<APIResponse> {
    const token = await csrfToken(page, warmPath);
    return page.request.post(path, {
        form: { csrfmiddlewaretoken: token, ...form },
        headers: { "X-CSRFToken": token, Origin: env.baseUrl, Referer: `${env.baseUrl}${warmPath ?? "/dashboard/"}` },
        maxRedirects: 0,
    });
}

export const pinShareRoutes = {
    dialog: (pinSlug: string) => `/dashboard/map/pin/${pinSlug}/share/`,
    send: (pinSlug: string) => `/dashboard/map/pin/${pinSlug}/share/send/`,
    detail: (shareId: number) => `/dashboard/pin-shares/${shareId}/`,
    respond: (shareId: number) => `/dashboard/pin-shares/${shareId}/respond/`,
    received: "/dashboard/memories/sharing/received/",
};

/** Profile pk of `username` as listed in the sender's share dialog, which only lists accepted friends. */
export async function friendProfileId(senderPage: Page, pinSlug: string, username: string): Promise<string> {
    const response = await senderPage.request.get(pinShareRoutes.dialog(pinSlug));
    expect(response.status(), `the share dialog for ${pinSlug} answered ${response.status()}`).toBe(200);
    const html = await response.text();
    for (const match of html.matchAll(/data-id="(\d+)"\s+data-username="([^"]*)"/g)) {
        if (match[2] === username && match[1]) {
            return match[1];
        }
    }
    throw new Error(`the share dialog does not list "${username}" as a friend; ensureFriends() should have made them one`);
}

/** Sends `pinSlug` to profile `profileId` from the sender's session. */
export async function postPinShare(senderPage: Page, pinSlug: string, profileId: string, extra: Record<string, string> = {}): Promise<APIResponse> {
    return sessionPost(senderPage, pinShareRoutes.send(pinSlug), { profile_id: profileId, custom_name: "", message: "", ...extra }, pinShareRoutes.dialog(pinSlug));
}

/** Share ids linked from page 1 of the recipient's "Shared with you" list, newest place first. */
export async function receivedShareIds(recipientPage: Page): Promise<Set<number>> {
    const response = await recipientPage.request.get(pinShareRoutes.received);
    expect(response.status(), `the received-shares list answered ${response.status()}`).toBe(200);
    const html = await response.text();
    return new Set([...html.matchAll(/\/dashboard\/pin-shares\/(\d+)\//g)].map((match) => Number(match[1])));
}

/** The snapshotted coordinates a share detail page centres its map on, or null. */
export function sharedCoordinates(html: string): { lat: number; lng: number } | null {
    const lat = /const lat = (-?\d+(?:\.\d+)?)\s*;/.exec(html)?.[1];
    const lng = /const lng = (-?\d+(?:\.\d+)?)\s*;/.exec(html)?.[1];
    return lat && lng ? { lat: Number(lat), lng: Number(lng) } : null;
}

const COORDINATE_TOLERANCE = 1e-5;

/** Stored coordinates of a pin, as its owner's API reports them. */
export async function storedCoordinates(ownerApi: ApiClient, pinSlug: string): Promise<{ lat: number; lng: number }> {
    const detail = await ownerApi.json<{ latitude: number; longitude: number }>("get", `pins/${pinSlug}/`);
    return { lat: Number(detail.latitude), lng: Number(detail.longitude) };
}

/**
 * Share ids that appeared since `before` whose detail page is centred on `at`.
 *
 * Matched by the snapshotted coordinates rather than by name, because the name a preview shows is
 * the very thing under test.
 */
export async function newSharesAt(recipientPage: Page, before: Set<number>, at: { lat: number; lng: number }): Promise<number[]> {
    const matches: number[] = [];
    for (const id of await receivedShareIds(recipientPage)) {
        if (before.has(id)) {
            continue;
        }
        const detail = await recipientPage.request.get(pinShareRoutes.detail(id));
        if (detail.status() !== 200) {
            continue;
        }
        const coords = sharedCoordinates(await detail.text());
        if (coords && Math.abs(coords.lat - at.lat) < COORDINATE_TOLERANCE && Math.abs(coords.lng - at.lng) < COORDINATE_TOLERANCE) {
            matches.push(id);
        }
    }
    return matches;
}

export interface DeliveredShare {
    shareId: number;
    at: { lat: number; lng: number };
}

/**
 * Shares `pin` from the sender's session and returns the id the recipient received it under.
 *
 * Asserts delivery: the send answered 200, exactly one new share at the pin's coordinates reached
 * the recipient, and it is still awaiting a decision.
 */
export async function sharePin(
    senderApi: ApiClient,
    senderPage: Page,
    recipientPage: Page,
    pin: CreatedPin,
    recipientUsername: string,
    extra: Record<string, string> = {},
): Promise<DeliveredShare> {
    const at = await storedCoordinates(senderApi, pin.slug);
    const profileId = await friendProfileId(senderPage, pin.slug, recipientUsername);
    const before = await receivedShareIds(recipientPage);

    const sent = await postPinShare(senderPage, pin.slug, profileId, extra);
    expect(sent.status(), `sending the share answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBe(200);
    expect(await sent.text(), "the share dialog did not confirm the send").toContain(`was shared with ${recipientUsername}`);

    const delivered = await newSharesAt(recipientPage, before, at);
    expect(delivered, `expected exactly one new share at ${at.lat},${at.lng} in the recipient's list, found ${delivered.length}`).toHaveLength(1);
    const shareId = delivered[0] as number;

    const detail = await (await recipientPage.request.get(pinShareRoutes.detail(shareId))).text();
    expect(detail, "the delivered share is not awaiting a decision (the recipient may already have a pin there)").toContain("Accept and add to my map");
    return { shareId, at };
}

/** Accepts a share through the external API and registers the recipient's new pin for cleanup. */
export async function acceptShare(recipientApi: ApiClient, shareId: number): Promise<string> {
    const response = await recipientApi.post(`pin-shares/${shareId}/respond/`, { action: "accept" });
    expect(response.status(), `accepting share ${shareId} answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
    const body = (await response.json()) as { status: string; pin_slug: string | null };
    expect(body.status, "the accept response does not report the share as accepted").toBe("accepted");
    expect(body.pin_slug, "accepting produced no recipient-side pin").toBeTruthy();
    const copySlug = body.pin_slug as string;
    recipientApi.track("pin", copySlug, () => recipientApi.delete(`pins/${copySlug}/`));
    return copySlug;
}
