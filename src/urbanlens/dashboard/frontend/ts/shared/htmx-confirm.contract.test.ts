/**
 * An htmx request is confirmed with ``hx-confirm``, never an inline ``confirm()`` or ``data-confirm``.
 *
 * htmx 1.9 sends the request whether or not the submit was cancelled: its trigger listener never reads
 * ``defaultPrevented``. So ``onsubmit="return confirm(...)"`` on an ``hx-post`` form asked, and a Cancel went
 * ahead anyway. Dismissing "Remove your authenticator app?" removed it.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..", "..", "..");
// A template tag inside an attribute has its own ">", so step over {% %} and {{ }} whole.
const ATTRS = String.raw`(?:\{%.*?%\}|\{\{.*?\}\}|[^>])*`;
const TAG = new RegExp(String.raw`<(?:form|button|a|input|div|span|li|tr|select|textarea)\b${ATTRS}>`, "gs");
const HTMX_FORM = new RegExp(String.raw`<form\b${ATTRS}>.*?</form>`, "gs");
const HTMX_REQUEST = /\bhx-(?:get|post|put|patch|delete)\s*=/;
const LATE_CONFIRM = /\bon(?:submit|click)\s*=\s*"[^"]*\bconfirm\(|\bdata-confirm\s*=/;

/** Elements that issue an htmx request and confirm some other way, including a confirm on a control inside an htmx form. */
function offenders(markup: string): string[] {
    const found: string[] = [];
    for (const tag of markup.match(TAG) ?? []) {
        if (HTMX_REQUEST.test(tag) && LATE_CONFIRM.test(tag)) found.push(tag);
    }
    for (const form of markup.match(HTMX_FORM) ?? []) {
        const open = form.match(TAG)?.[0] ?? "";
        if (!HTMX_REQUEST.test(open)) continue;
        for (const inner of form.slice(open.length).match(TAG) ?? []) {
            if (LATE_CONFIRM.test(inner)) found.push(`${open} ... ${inner}`);
        }
    }
    return found;
}

describe("htmx confirmations", () => {
    test("the check sees each shape of the bug", () => {
        expect(offenders(`<form hx-post="/x/" onsubmit="return confirm('Sure?')"></form>`).length).toBe(1);
        expect(offenders(`<button hx-delete="/x/" data-confirm="Sure?">Go</button>`).length).toBe(1);
        expect(offenders(`<form hx-post="/x/" hx-target="#y"><button type="submit" onclick="return confirm('Sure?')">Delete</button></form>`).length).toBe(1);
        expect(offenders(`<form hx-post="{% url 'x' %}"><button data-confirm="Sure?">Delete</button></form>`).length).toBe(1);
        expect(offenders(`<form method="post"><button data-confirm="Sure?">Delete</button></form><form hx-post="/y/"><button>Save</button></form>`)).toEqual([]);
        expect(offenders(`<form hx-post="/x/" hx-confirm="Sure?"><button>Delete</button></form>`)).toEqual([]);
    });

    test("no template confirms an htmx request outside hx-confirm", () => {
        const found: string[] = [];
        for (const path of new Bun.Glob("src/urbanlens/dashboard/templates/**/*.html").scanSync(REPO_ROOT)) {
            for (const hit of offenders(readFileSync(join(REPO_ROOT, path), "utf8"))) found.push(`${path}: ${hit.replace(/\s+/g, " ").slice(0, 160)}`);
        }
        expect(found).toEqual([]);
    });
});
