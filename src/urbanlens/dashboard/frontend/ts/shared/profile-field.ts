/**
 * One profile field at a time, against ``profile.field.update`` (``controllers/userprofile.py``): the
 * username check, a field save, and the avatar choices. The setup wizard and the profile edit page both use it.
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

/** Show a new avatar in *el*'s place: its ``src`` when it is an image, else an image replacing the placeholder. */
export function showAvatar(el: Element | null, url: string, className: string): void {
    if (!el) return;
    const src = `${url}?${Date.now()}`;
    if (el instanceof HTMLImageElement) {
        el.src = src;
        return;
    }
    const img = document.createElement("img");
    img.src = src;
    img.id = el.id;
    img.alt = "Avatar";
    img.className = className;
    el.replaceWith(img);
}

export type AvatarChoice = { kind: "upload"; file: File } | { kind: "gravatar" } | { kind: "emoji"; animal: string; color: string };

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
