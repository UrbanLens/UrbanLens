/**
 * JSON requests that fail loudly.
 *
 * ``fetch`` resolves for 400s and 500s - it only rejects when the request never
 * completed - so ``fetch(...).then(r => r.json())`` treats a rejected write as a
 * success. Several call sites did exactly that and reported "Rating saved" or
 * "Updated successfully" for requests the server had refused.
 *
 * Everything here throws on a non-2xx, carrying the server's own message when it
 * sent one, so a caller's ``catch`` can say something more useful than "failed".
 *
 * Promoted out of the map page's private ``_fetchJson``, which already had the
 * ``!resp.ok`` check the mutating calls in the very same file were missing.
 */

export interface FetchJsonOptions extends RequestInit {
    /** Abort after this long. Default two minutes, matching the map's tile fetches. */
    timeoutMs?: number;
}

export class HttpError extends Error {
    readonly status: number;

    constructor(status: number, message: string) {
        super(message);
        this.name = "HttpError";
        this.status = status;
    }
}

/** Longest plain-text body still worth putting in a toast rather than discarding. */
const MAX_PLAIN_TEXT_MESSAGE = 200;

/**
 * A body that is a short line of prose, not a document.
 *
 * Many of this project's views answer a refused write with a bare
 * ``HttpResponse("Select at most 500 pins at a time.", status=400)`` rather than
 * JSON - which is exactly the sentence the user needs. Only markup and
 * paragraphs are worth discarding.
 */
function isReadableText(text: string): boolean {
    return text.length <= MAX_PLAIN_TEXT_MESSAGE && !text.includes("<") && !text.includes("\n");
}

/** Pull a human-readable message out of an error response, if there is one. */
async function errorMessage(response: Response): Promise<string> {
    const fallback = `HTTP ${response.status}`;
    try {
        const text = (await response.text()).trim();
        if (!text) return fallback;
        try {
            const data: unknown = JSON.parse(text);
            if (typeof data === "string") return data || fallback;
            if (data && typeof data === "object") {
                const record = data as Record<string, unknown>;
                // DRF uses "detail"; this project's views use "error" or "message".
                for (const key of ["detail", "error", "message"]) {
                    const value = record[key];
                    if (typeof value === "string" && value) return value;
                }
            }
            return fallback;
        } catch {
            // Not JSON. A plain-text refusal is the message; an HTML error page
            // is markup that would be nonsense in a toast.
            return isReadableText(text) ? text : fallback;
        }
    } catch {
        return fallback;
    }
}

export async function fetchJson<T = unknown>(url: string, options: FetchJsonOptions = {}): Promise<T | null> {
    const { timeoutMs = 120000, ...init } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
        const response = await fetch(url, { ...init, signal: controller.signal });
        if (!response.ok) throw new HttpError(response.status, await errorMessage(response));
        // 204 has no body, and calling .json() on it throws. Callers that expect
        // nothing back (a recorded position, a DRF delete) would otherwise see a
        // successful request as a failure.
        if (response.status === 204) return null;
        return (await response.json()) as T;
    } finally {
        clearTimeout(timer);
    }
}

/** Send JSON with the CSRF header Django requires for unsafe methods. */
export async function sendJson<T = unknown>(url: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown, options: FetchJsonOptions = {}): Promise<T | null> {
    const { headers, ...rest } = options;
    return fetchJson<T>(url, {
        method,
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.csrftoken ?? "",
            ...(headers as Record<string, string> | undefined),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        ...rest,
    });
}

declare global {
    interface Window {
        ulFetchJson?: typeof fetchJson;
        ulSendJson?: typeof sendJson;
    }
}

export function installGlobalFetchJson(): void {
    // Exposed because every caller is inline template script, which cannot import.
    window.ulFetchJson = fetchJson;
    window.ulSendJson = sendJson;
}
