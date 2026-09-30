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
const TAG = /<(?:form|button|a|input|div|span|li|tr|select|textarea)\b(?:\{%.*?%\}|\{\{.*?\}\}|[^>])*>/gs;
const HTMX_REQUEST = /\bhx-(?:get|post|put|patch|delete)\s*=/;
const LATE_CONFIRM = /\bon(?:submit|click)\s*=\s*"[^"]*\bconfirm\(|\bdata-confirm\s*=/;

describe("htmx confirmations", () => {
    test("no element that issues an htmx request confirms outside hx-confirm", () => {
        const offenders: string[] = [];
        for (const path of new Bun.Glob("src/urbanlens/dashboard/templates/**/*.html").scanSync(REPO_ROOT)) {
            for (const tag of readFileSync(join(REPO_ROOT, path), "utf8").match(TAG) ?? []) {
                if (HTMX_REQUEST.test(tag) && LATE_CONFIRM.test(tag)) offenders.push(`${path}: ${tag.replace(/\s+/g, " ").slice(0, 120)}`);
            }
        }
        expect(offenders).toEqual([]);
    });
});
