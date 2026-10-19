/**
 * Google's Programmable Search Element on the pin page's web-search panel.
 *
 * The Element runs in the viewer's browser, so Google sees the viewer's address and not the server's. The
 * panel renders it only for viewers who have web search and external APIs enabled. When a search returns,
 * its results are posted to the server to be cached as user-provided, lower-trust results.
 */

import { getCsrfToken } from "./csrf";

const SCRIPT_ID = "google-pse-script";
const GNAME = "ul-pin-pse";
const MAX_POSTED = 20;

interface CseResult {
    unescapedUrl?: string;
    url?: string;
    titleNoFormatting?: string;
    contentNoFormatting?: string;
}

interface CseElement {
    execute(query: string): void;
}

interface CseApi {
    render(options: { div: Element; tag: string; gname: string; attributes?: Record<string, unknown> }): void;
    getElement(gname: string): CseElement | undefined;
}

interface GcseConfig {
    parsetags?: string;
    initializationCallback?: () => void;
    searchCallbacks?: { web?: { ready?: (gname: string, query: string, promos: unknown, results: unknown) => boolean } };
}

declare global {
    interface Window {
        __gcse?: GcseConfig;
        google?: { search?: { cse?: { element?: CseApi } } };
    }
}

export interface PostedResult {
    url: string;
    title: string;
    snippet: string;
}

/** http(s) results only, bounded, in the shape the endpoint accepts. */
export function toPostedResults(results: unknown): PostedResult[] {
    if (!Array.isArray(results)) return [];
    const out: PostedResult[] = [];
    for (const raw of results as CseResult[]) {
        const url = raw?.unescapedUrl ?? raw?.url;
        if (typeof url !== "string" || !/^https?:\/\//i.test(url)) continue;
        out.push({ url, title: String(raw.titleNoFormatting ?? ""), snippet: String(raw.contentNoFormatting ?? "") });
        if (out.length >= MAX_POSTED) break;
    }
    return out;
}

async function postResults(postUrl: string, results: PostedResult[]): Promise<void> {
    if (results.length === 0) return;
    try {
        await fetch(postUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify({ results }),
        });
    } catch {
        // Caching is a courtesy; the viewer already has their results.
    }
}

function elementApi(): CseApi | undefined {
    return window.google?.search?.cse?.element;
}

function startSearch(host: HTMLElement): void {
    const target = host.querySelector(".web-search-pse-results");
    const api = elementApi();
    const query = host.dataset.pseQuery ?? "";
    if (!target || !api || !query) return;
    api.render({ div: target, tag: "searchresults-only", gname: GNAME });
    api.getElement(GNAME)?.execute(query);
}

/** Set up the Element in *host* (a `[data-google-pse]` element), loading Google's script on first use. */
export function installGooglePse(host: HTMLElement): void {
    if (host.dataset.pseStarted === "1") return;
    const cx = host.dataset.pseCx ?? "";
    const postUrl = host.dataset.psePostUrl ?? "";
    if (!/^[A-Za-z0-9:_-]{1,128}$/.test(cx) || !postUrl) return;
    host.dataset.pseStarted = "1";

    window.__gcse = {
        parsetags: "explicit",
        initializationCallback: () => startSearch(host),
        searchCallbacks: {
            web: {
                ready: (_gname, _query, _promos, results) => {
                    void postResults(postUrl, toPostedResults(results));
                    return false;
                },
            },
        },
    };

    if (elementApi()) {
        startSearch(host);
        return;
    }
    if (document.getElementById(SCRIPT_ID)) return;
    const script = document.createElement("script");
    script.id = SCRIPT_ID;
    script.async = true;
    script.src = `https://cse.google.com/cse.js?cx=${encodeURIComponent(cx)}`;
    document.head.appendChild(script);
}

/** Start every Element under *root*; called after the panel is swapped in. */
export function installGooglePseIn(root: ParentNode): void {
    root.querySelectorAll<HTMLElement>("[data-google-pse]").forEach(installGooglePse);
}
