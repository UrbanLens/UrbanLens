/**
 * A pin's detail page.
 *
 * The busiest page in the application: it resolves boundaries, asks REData
 * about the parcel, looks for a wiki, loads enrichment panels contributed by
 * plugins, and renders its own map. That makes it the single best page to point
 * an integration run at, because almost every dependent service is reachable
 * from it - and the single most likely place for one of them to fail quietly.
 */

import { expect, type Locator, type Page } from "@playwright/test";

import { pinDetail } from "../routes.js";

/** The tabs across the top of the page, by their `data-tab` value. */
export type PinTab = "overview" | "visits" | "photos" | "article" | "comments" | "history";

export class PinDetailPage {
    readonly hero: Locator;
    readonly content: Locator;
    readonly tabs: Locator;

    constructor(private readonly page: Page) {
        this.hero = page.locator("#pin-detail-hero");
        this.content = page.locator(".location-detail-page");
        this.tabs = page.locator(".ul-subnav-tab");
    }

    async goto(slug: string): Promise<void> {
        await this.page.goto(pinDetail(slug));
        await this.expectLoaded();
    }

    async expectLoaded(): Promise<void> {
        await expect(this.hero).toBeVisible();
        await expect(this.content).toBeVisible();
    }

    /** The tab button for `name`. */
    tab(name: PinTab): Locator {
        return this.page.locator(`.ul-subnav-tab[data-tab="${name}"]`);
    }

    /**
     * Switches to `name` and waits for its panel to become the active one.
     *
     * Tab switching is client-side (`page-tabs.js`), so there is no navigation
     * or HTMX exchange to wait on - only the class change.
     */
    async openTab(name: PinTab): Promise<void> {
        const button = this.tab(name);
        await button.click();
        await expect(button).toHaveClass(/is-active/);
    }
}
