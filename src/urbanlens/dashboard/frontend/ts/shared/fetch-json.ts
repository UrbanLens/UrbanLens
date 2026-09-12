/**
 * HTTP requests that fail loudly.
 */

export interface FetchJsonOptions extends RequestInit {
    /** Abort after this long. Default two minutes, matching the map's tile fetches. */
    timeoutMs?: number;

    /**
 * This caller shows the user its own message, so suppress the generic one.
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
    // 204 has no body, and calling .json() on it throws.
    return requestBody<T | null>(url, options, async (response) => (response.status === 204 ? null : ((await response.json()) as T)));
}

/**
 * The same request, for an endpoint that answers with markup rather than JSON.
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
