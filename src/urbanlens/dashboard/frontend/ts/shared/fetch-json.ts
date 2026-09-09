/**
 * HTTP requests that fail loudly.
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

    /**
     * This caller shows the user its own message, so suppress the generic one.
     *
     * `themes/base.html` wraps `window.fetch` and toasts "Request failed (HTTP
     * 503)." for any non-2xx - the net under the ~90 raw `fetch()` call sites
     * that have not moved yet (P11). Throwing an `HttpError` carrying the
     * server's own sentence does not, by itself, mean anyone said anything: most
     * callers here are inline template scripts that only `console.warn`, and the
     * generic toast is the only thing a user sees.
     *
     * So this is opt-in rather than automatic. Set it where the caller really
     * does report - `session-request.ts` does - and a single refusal is
     * announced once instead of twice. Leave it off and the net stays in place,
     * which is what an unmigrated call site needs.
     *
     * The first version of this applied it inside `fetchJson` for everyone,
     * which turned a generic toast into silence on every page that had not been
     * migrated - including the map's cold-start pin load, whose only handler is
     * a `console.warn`.
     */
    reportsItsOwnErrors?: boolean;
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

/**
 * Make the request under the timeout, throw on a non-2xx, and read the body.
 *
 * The half that is the same whether the caller wants JSON or markup back -
 * which is most of it, since the value of this module is the ``!response.ok``
 * check and the message extraction rather than the parsing.
 *
 * ``read`` runs inside the same ``try`` as the fetch on purpose. Headers can
 * land promptly and the body still stall - a large payload behind a slow proxy,
 * a connection that dies mid-stream - and a real ``Response`` rejects its body
 * read when the signal aborts. Clearing the timer as soon as the status was
 * known left that read with nothing to abort it, so the promise hung forever:
 * no toast, no rejection, nothing in the console. Exactly the shape the album
 * upload's ten-minute ceiling exists for.
 */
async function requestBody<T>(url: string, options: FetchJsonOptions, read: (response: Response) => Promise<T>): Promise<T> {
    const { timeoutMs = 120000, reportsItsOwnErrors = false, ...init } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
        // `__ulReported` is what base.html's wrapper reads; `fetch` ignores it.
        const response = await fetch(url, { ...init, __ulReported: reportsItsOwnErrors, signal: controller.signal } as RequestInit);
        if (!response.ok) throw new HttpError(response.status, await errorMessage(response));
        return await read(response);
    } finally {
        clearTimeout(timer);
    }
}

export async function fetchJson<T = unknown>(url: string, options: FetchJsonOptions = {}): Promise<T | null> {
    // 204 has no body, and calling .json() on it throws. Callers that expect
    // nothing back (a recorded position, a DRF delete) would otherwise see a
    // successful request as a failure.
    return requestBody<T | null>(url, options, async (response) => (response.status === 204 ? null : ((await response.json()) as T)));
}

/**
 * The same request, for an endpoint that answers with markup rather than JSON.
 *
 * Several views here return a rendered fragment the caller swaps into the DOM
 * (Organize's bulk delete/edit, which re-render the row list). Those call sites
 * could not use ``fetchJson`` at all, so each grew its own wrapper with its own
 * idea of what a failed request looks like - which is what this exists to stop.
 *
 * Returns the body as text, empty string included: a fragment endpoint that
 * legitimately renders nothing is not an error.
 */
export async function fetchText(url: string, options: FetchJsonOptions = {}): Promise<string> {
    return requestBody(url, options, (response) => response.text());
}

/** The JSON body and CSRF header Django requires for an unsafe method. */
function writeInit(method: string, body: unknown, options: FetchJsonOptions): FetchJsonOptions {
    const { headers, ...rest } = options;
    return {
        method,
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.csrftoken ?? "",
            ...(headers as Record<string, string> | undefined),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        ...rest,
    };
}

/** Send JSON with the CSRF header Django requires for unsafe methods. */
export async function sendJson<T = unknown>(url: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown, options: FetchJsonOptions = {}): Promise<T | null> {
    return fetchJson<T>(url, writeInit(method, body, options));
}

/** Send JSON to an endpoint that answers with markup. */
export async function sendForText(url: string, method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown, options: FetchJsonOptions = {}): Promise<string> {
    return fetchText(url, writeInit(method, body, options));
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
