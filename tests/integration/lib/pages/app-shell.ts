/**
 * The chrome every signed-in page shares: navigation, user menu, notifications.
 *
 * Worth a page object of its own because it is the cheapest evidence that a
 * page rendered *as a signed-in user* rather than merely returning 200 - a
 * session that quietly expired serves the anonymous variant of several pages
 * with a 200 and no visible complaint.
 */

import { expect, type Locator, type Page } from "@playwright/test";

export class AppShell {
    readonly nav: Locator;
    readonly navLinks: Locator;
    readonly brand: Locator;
    readonly userButton: Locator;
    readonly userDropdown: Locator;
    readonly userName: Locator;
    readonly notificationsButton: Locator;
    readonly messagesButton: Locator;
    readonly searchButton: Locator;
    readonly notificationsPanel: Locator;
    readonly hamburger: Locator;
    readonly mobileDrawer: Locator;

    constructor(private readonly page: Page) {
        this.nav = page.locator("nav.app-nav");
        this.navLinks = this.nav.locator("a.app-nav-link");
        this.brand = this.nav.locator("a.app-nav-brand");
        this.userButton = page.locator("#nav-user-btn");
        this.userDropdown = page.locator("#nav-dropdown");
        this.userName = this.userButton.locator(".nav-user-name");
        this.notificationsButton = page.locator("#nav-notif-btn");
        this.messagesButton = page.locator("#nav-msg-btn");
        this.searchButton = page.locator("#nav-search-btn");
        this.notificationsPanel = page.locator("#notif-dropdown-wrap");
        this.hamburger = page.locator("#nav-hamburger-btn");
        this.mobileDrawer = page.locator("#app-nav-drawer");
    }

    /** Asserts the page rendered signed in, as `username`. */
    async expectSignedInAs(username: string): Promise<void> {
        await expect(this.userButton).toBeVisible();
        await expect(this.userName).toHaveText(username);
    }

    /** Opens the account dropdown. */
    async openUserMenu(): Promise<void> {
        await this.userButton.click();
        await expect(this.userDropdown).toBeVisible();
    }

    /**
     * Every destination the primary navigation offers, deduplicated.
     *
     * Discovered from the rendered menu rather than listed in the suite, so the
     * "nothing in the navigation is broken" sweep covers a page added later
     * without anyone remembering to add it here too.
     */
    async navigationTargets(): Promise<Array<{ label: string; href: string }>> {
        const links = await this.navLinks.all();
        const targets = new Map<string, string>();
        for (const link of links) {
            const href = await link.getAttribute("href");
            const label = (await link.innerText()).trim();
            if (href && href.startsWith("/") && !targets.has(href)) {
                targets.set(href, label);
            }
        }
        return [...targets].map(([href, label]) => ({ href, label }));
    }
}
