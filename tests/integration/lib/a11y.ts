/**
 * Accessibility scanning, via axe-core.
 *
 * Two deliberate choices about severity. First, only `serious` and `critical`
 * findings fail a test by default: a suite that goes red on every
 * colour-contrast near-miss gets muted wholesale within a week, and then
 * catches nothing at all. Second, the findings below that threshold are still
 * attached to the report, so the moderate ones are visible to anyone reading a
 * run rather than being discarded.
 *
 * Scans run against the deployed page, so they see the real rendered DOM
 * including anything HTMX has swapped in - which is most of this application's
 * interactive surface, and the part a static template check cannot reach.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import type { Result as AxeResult } from "axe-core";

/** Severity ordering, lowest first. */
const IMPACT_ORDER = ["minor", "moderate", "serious", "critical"] as const;
export type Impact = (typeof IMPACT_ORDER)[number];

/**
 * WCAG rule sets applied by default.
 *
 * `best-practice` is deliberately excluded: it holds opinions (heading order,
 * landmark uniqueness) that are worth knowing but are not conformance
 * failures, and mixing them in makes the pass/fail line arbitrary.
 */
const DEFAULT_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

export interface ScanOptions {
    /** CSS selector to restrict the scan to. Defaults to the whole page. */
    include?: string;
    /** CSS selectors to skip - third-party widgets, mostly. */
    exclude?: string[];
    /** Rule ids to switch off for this scan, each with a reason in the spec. */
    disableRules?: string[];
    /** Lowest impact that fails the test. Defaults to `serious`. */
    failOn?: Impact;
    /** Override the WCAG tag set. */
    tags?: string[];
}

/**
 * Rules disabled everywhere, with the reason each one is not actionable here.
 *
 * Anything added to this list is a defect that will never be found again, so
 * each entry needs to be a genuine false positive rather than a backlog item.
 */
const GLOBALLY_DISABLED: Record<string, string> = {
    // Leaflet builds its tile layer out of positioned <img> elements with empty
    // alt text and its own ARIA handling. axe flags the panes as unlabelled
    // regions; the map itself carries the label.
    "region": "Leaflet map panes are reported as unlabelled regions on every map page.",
};

/**
 * Rules that are still *run* and still *reported*, but never fail a test.
 *
 * Distinct from {@link GLOBALLY_DISABLED} above, and the distinction matters:
 * those are false positives, these are real findings that are simply not new.
 * A rule belongs here when it fires across the whole application today, because
 * the alternative is a project that is red on every run from the day it is
 * written - and a suite nobody trusts catches nothing at all.
 *
 * Findings still appear in the report's `a11y-advisory.txt`, so the backlog
 * stays visible rather than being deleted. Remove an entry once the underlying
 * issue is fixed, and the rule starts guarding against regressions.
 */
const ADVISORY_RULES: Record<string, string> = {
    "color-contrast": "The palette misses AA on secondary text and on the social sign-in buttons, on every page. Tracked as design work, not as a regression.",
};

function formatViolations(violations: AxeResult[]): string {
    return violations
        .map((violation) => {
            const nodes = violation.nodes
                .slice(0, 5)
                .map((node) => `      ${node.target.join(" ")}\n        ${node.failureSummary?.replace(/\n/g, "\n        ") ?? ""}`)
                .join("\n");
            const extra = violation.nodes.length > 5 ? `\n      ... and ${violation.nodes.length - 5} more element(s)` : "";
            return `  [${violation.impact ?? "unknown"}] ${violation.id}: ${violation.help}\n    ${violation.helpUrl}\n${nodes}${extra}`;
        })
        .join("\n\n");
}

/** Runs axe against `page` and returns every violation it found. */
export async function scanAccessibility(page: Page, options: ScanOptions = {}): Promise<AxeResult[]> {
    let builder = new AxeBuilder({ page }).withTags(options.tags ?? DEFAULT_TAGS);

    if (options.include) {
        builder = builder.include(options.include);
    }
    for (const selector of options.exclude ?? []) {
        builder = builder.exclude(selector);
    }
    builder = builder.disableRules([...Object.keys(GLOBALLY_DISABLED), ...(options.disableRules ?? [])]);

    const results = await builder.analyze();
    return results.violations;
}

/**
 * Scans `page` and fails the test on anything at or above `failOn`.
 *
 * Lower-severity findings are attached to the report under
 * `a11y-advisory.txt` rather than being dropped.
 */
export async function expectAccessible(page: Page, options: ScanOptions = {}): Promise<void> {
    const threshold = IMPACT_ORDER.indexOf(options.failOn ?? "serious");
    const violations = await scanAccessibility(page, options);

    const blocking = violations.filter(
        (violation) => !(violation.id in ADVISORY_RULES) && IMPACT_ORDER.indexOf((violation.impact ?? "minor") as Impact) >= threshold,
    );
    const advisory = violations.filter((violation) => !blocking.includes(violation));

    if (advisory.length > 0) {
        await test.info().attach("a11y-advisory.txt", {
            body: `Not failing this test on ${page.url()} - below the severity threshold, or a known-outstanding rule:\n\n${formatViolations(advisory)}`,
            contentType: "text/plain",
        });
    }

    expect(blocking, `Accessibility violations on ${page.url()}:\n\n${formatViolations(blocking)}`).toHaveLength(0);
}
