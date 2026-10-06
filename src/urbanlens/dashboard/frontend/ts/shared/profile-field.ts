/**
 * One profile field at a time, against ``profile.field.update`` (``controllers/userprofile.py``): the
 * username check, a field save, and the avatar choices. The setup wizard and the profile edit page both use it.
 *
 * An avatar is drawn in several places (the page hero, the form, the navbar). Each carries
 * ``data-user-avatar="<class>"`` - the class it has as an image - and :func:`showUserAvatar` redraws them all.
 */

import { getCsrfToken } from "./csrf";
import { fetchJson, HttpError } from "./fetch-json";

export const USERNAME_PATTERN = /^[a-zA-Z0-9_]{3,30}$/;
export const USERNAME_RULE = "3-30 chars: letters, numbers, underscores";

export interface FieldResult {
    ok: boolean;
    /** The server turned the value down (a 4xx), as opposed to the save not getting through. */
    refused?: boolean;
    /** The server's own words, when it gave any. */
    error?: string;
    avatar_url?: string | null;
    avatar_pending?: boolean;
}

interface Availability {
    available?: boolean;
    reason?: string;
}

/**
 * Save *field*. A refusal comes back as ``{ok: false, refused: true, error}`` whether the server said so with a
 * 4xx or, as the date fields do, with a 200.
 */
export async function saveProfileField(url: string, field: string, value?: string | File, extra: Record<string, string> = {}): Promise<FieldResult> {
    const body = new FormData();
    body.append("field", field);
    if (value instanceof File) body.append("file_value", value);
    else if (value !== undefined) body.append("value", value);
    for (const [key, val] of Object.entries(extra)) body.append(key, val);
    try {
        const data = await fetchJson<FieldResult & { error?: string }>(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
            body,
            reportsItsOwnErrors: true,
        });
        return data?.ok ? data : { ok: false, refused: true, error: data?.error };
    } catch (err) {
        if (!(err instanceof HttpError)) return { ok: false };
        // fetchJson falls back to "HTTP <status>" when the body said nothing readable.
        const error = err.message === `HTTP ${err.status}` ? undefined : err.message;
        return { ok: false, refused: err.status >= 400 && err.status < 500, error };
    }
}

/** Whether *username* is free, or why not. Null when the check itself failed. */
export async function usernameAvailability(url: string, username: string): Promise<{ available: boolean; reason: string } | null> {
    try {
        const data = await fetchJson<Availability>(`${url}?field=username&value=${encodeURIComponent(username)}`, { reportsItsOwnErrors: true });
        return { available: !!data?.available, reason: data?.reason || "Unavailable" };
    } catch {
        return null;
    }
}

/**
 * *url* with a ``v`` parameter, so a browser that cached the previous picture at the same address asks again.
 * @param url - A same-origin path or an absolute URL; a ``blob:`` or ``data:`` URL is already unique and is returned as is.
 * @param version - The value of ``v``.
 * @returns The URL, its other parameters kept.
 */
export function withVersion(url: string, version: number = Date.now()): string {
    if (/^(blob|data):/i.test(url)) return url;
    const parsed = new URL(url, window.location.href);
    parsed.searchParams.set("v", String(version));
    return parsed.origin === window.location.origin ? parsed.pathname + parsed.search + parsed.hash : parsed.toString();
}

/**
 * Show a new avatar in *el*'s place: its ``src`` when it is an image, else an image replacing the placeholder.
 * @param el - The image, or the placeholder (initials, an icon) standing in for one.
 * @param url - The picture.
 * @param className - The class the replacing image takes; ``data-user-avatar`` and ``data-avatar-alt`` carry over.
 * @param alt - The replacing image's alternative text.
 */
export function showAvatar(el: Element | null, url: string, className: string, alt = "Avatar"): void {
    if (!el) return;
    const src = withVersion(url);
    if (el instanceof HTMLImageElement) {
        el.src = src;
        return;
    }
    const img = document.createElement("img");
    img.src = src;
    img.id = el.id;
    img.alt = alt;
    img.className = className;
    for (const name of ["data-user-avatar", "data-avatar-alt"]) {
        const value = el.getAttribute(name);
        if (value !== null) img.setAttribute(name, value);
    }
    el.replaceWith(img);
}

/** Every element drawing the signed-in user's avatar. */
const USER_AVATARS = "[data-user-avatar]";

/**
 * Draw *url* as the signed-in user's avatar everywhere the page does: the hero, the form, the navbar.
 * @param url - The picture. A browser that cached the previous one at the same address is made to ask again.
 */
export function showUserAvatar(url: string): void {
    for (const el of document.querySelectorAll<HTMLElement>(USER_AVATARS)) showAvatar(el, url, el.dataset.userAvatar ?? "", el.dataset.avatarAlt);
}

export type AvatarChoice = { kind: "upload"; file: File } | { kind: "gravatar"; previewUrl?: string } | { kind: "emoji"; animal: string; color: string };

/** Save an avatar choice. */
export function saveAvatar(url: string, choice: AvatarChoice): Promise<FieldResult> {
    switch (choice.kind) {
        case "upload":
            return saveProfileField(url, "avatar", choice.file);
        case "gravatar":
            return saveProfileField(url, "avatar_gravatar");
        case "emoji":
            return saveProfileField(url, "avatar_emoji", undefined, { animal: choice.animal, color: choice.color });
    }
}

/** What an avatar save's status says once it has worked. */
export function avatarSavedText(result: FieldResult): string {
    return result.avatar_pending ? "✓ Processing…" : "✓ Saved";
}

/** Types every browser draws; HEIC and TIFF, which the server accepts, are left for the stored copy. */
const PREVIEWABLE_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp"]);

/** How long to wait before each ask whether a held upload has been published. */
const PUBLISH_POLL_DELAYS_MS: readonly number[] = [1000, 1500, 2500, 4000, 6000, 8000, 10000];

/** Counts avatar choices, so the wait for an earlier one stops once a later one has been made. */
let latestAvatarChoice = 0;

/** What the page drew before the first of a run of held choices, for when none of them is stored and there is no picture to name. */
let baseline: Node[] | null = null;

/**
 * How a saved avatar choice ended up on the page.
 *
 * - ``shown``: the server had the new picture already (a suggested icon) and it is drawn.
 * - ``published``: the upload was held for processing, drawn from the user's own file meanwhile, and the stored copy has replaced it.
 * - ``unchanged``: the upload was held and then dropped (the file could not be processed); what is stored is back on show.
 * - ``waiting``: still not processed when the waiting ended; what was chosen stays on show.
 * - ``superseded``: a later choice was made.
 */
export type AvatarOutcome = "shown" | "published" | "unchanged" | "waiting" | "superseded";

interface AvatarState {
    avatar_url?: string | null;
    avatar_pending?: boolean;
}

/** The picture an upload or Gravatar choice shows before the server has processed it, or null when the browser cannot draw it. */
function localPreview(choice: AvatarChoice): string | null {
    if (choice.kind === "upload") return PREVIEWABLE_TYPES.has(choice.file.type) ? URL.createObjectURL(choice.file) : null;
    if (choice.kind === "gravatar") return choice.previewUrl || null;
    return null;
}

/** Put back what the page drew before the run of held choices began (a placeholder, when no picture was stored). */
function restoreBaseline(): void {
    document.querySelectorAll(USER_AVATARS).forEach((el, i) => {
        const before = baseline?.[i];
        if (before) el.replaceWith(before);
    });
}

/** Whether two addresses name the same stored file; a query string (a version, a signature) does not make it another. */
function sameFile(a: string | null | undefined, b: string | null | undefined): boolean {
    if (!a || !b) return a === b;
    const pathOf = (url: string): string => new URL(url, window.location.href).pathname;
    return pathOf(a) === pathOf(b);
}

async function storedAvatar(url: string): Promise<AvatarState | null> {
    try {
        return await fetchJson<AvatarState>(`${url}?field=avatar`, { reportsItsOwnErrors: true });
    } catch {
        return null;
    }
}

/**
 * Draw a saved avatar choice everywhere the page shows the user's avatar.
 *
 * A suggested icon is stored at once, so the response names its address. An upload or Gravatar is held until the
 * sandbox worker has re-encoded it, and the response then still names the picture it replaces (or nothing): the
 * choice is drawn from what the user picked, and the stored copy takes over once it exists.
 * @param url - The profile field endpoint.
 * @param choice - What was chosen.
 * @param result - The endpoint's answer to saving it.
 * @param options - ``sleep`` and ``delaysMs`` stand in for the real wait, in tests.
 * @returns How it ended up.
 */
export async function showSavedAvatar(
    url: string,
    choice: AvatarChoice,
    result: FieldResult,
    options: { sleep?: (ms: number) => Promise<void>; delaysMs?: readonly number[] } = {},
): Promise<AvatarOutcome> {
    const mine = ++latestAvatarChoice;
    if (!result.avatar_pending) {
        baseline = null;
        if (result.avatar_url) showUserAvatar(result.avatar_url);
        return "shown";
    }
    const preview = localPreview(choice);
    // Only the first of a run of held choices: a later one would otherwise take an earlier one's preview for what the page showed.
    baseline ??= Array.from(document.querySelectorAll(USER_AVATARS), (el) => el.cloneNode(true));
    if (preview) showUserAvatar(preview);
    const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms)));
    let outcome: AvatarOutcome = "waiting";
    for (const delay of options.delaysMs ?? PUBLISH_POLL_DELAYS_MS) {
        await sleep(delay);
        if (mine !== latestAvatarChoice) {
            outcome = "superseded";
            break;
        }
        const stored = await storedAvatar(url);
        if (!stored || stored.avatar_pending) continue;
        // A published upload is stored under a new name; the same one as before means it was dropped.
        if (stored.avatar_url && !sameFile(stored.avatar_url, result.avatar_url)) {
            showUserAvatar(stored.avatar_url);
            outcome = "published";
        } else {
            // What is stored is the truth, which an earlier choice of the run may have made differ from what the page began with.
            if (stored.avatar_url) showUserAvatar(stored.avatar_url);
            else restoreBaseline();
            outcome = "unchanged";
        }
        break;
    }
    if (outcome === "published" || outcome === "unchanged") baseline = null;
    // Left on show while the wait goes on, so only a finished one lets go of the user's file.
    if (preview?.startsWith("blob:") && outcome !== "waiting") URL.revokeObjectURL(preview);
    return outcome;
}
