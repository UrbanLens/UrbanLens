/**
 * Property Records ownership, gated on the `property_owners` feature, on the PRIVATE PIN DETAIL
 * page - not the wiki (see hrsh-property-data.spec.ts). Two accounts, two private pins at the same
 * coordinate, one card: services/property/owner_access.can_see_official_owners decides what each
 * one renders.
 */

import type { Page } from "@playwright/test";

import { hasFeature, PRIMARY_ROLE, PROPERTY_OWNERS_FEATURE, requireAccount, SUBSCRIBER_ROLE } from "../../lib/accounts.js";
import { KNOWN_OWNER_CANDIDATES } from "../../lib/hrsh.js";
import { recordMetric, type MetricTags } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";
import { waitForOrNull } from "../../lib/waiting.js";
import { expect, findOrCreateSubscriberPin, locationDataTest as test, openPrivatePin, skipUnlessLocationDataEnabled } from "./fixtures.js";

skipUnlessLocationDataEnabled();

const CARD_SELECTOR = "#property-records-section";
const OWNER_NAME_SELECTOR = `${CARD_SELECTOR} .simple-info-place-name`;
const CHIP_SELECTOR = `${CARD_SELECTOR} .simple-info-kind-chip`;
const META_SELECTOR = `${CARD_SELECTOR} .simple-info-meta-item`;
const LOCKED_CHIP_TEXT = "Owner on record - subscribers only";

/** Upper bound on the card's first load; a timeout here is a diagnosis, not a pass/fail on its own. */
const CARD_WAIT_TIMEOUT_MS = 600_000;
/** Gap between reloads; the panel already spends up to ~60s self-polling within each one (external_data.MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS). */
const CARD_RELOAD_GAP_MS = 5_000;

interface PropertyRecordsSnapshot {
    ownerName: string | null;
    chips: string[];
}

function cardDiagnosis(pinSlug: string): string {
    return (
        `#property-records-section on ${pinDetail(pinSlug)} never left its loading/pending state within ${CARD_WAIT_TIMEOUT_MS / 60_000} minutes. ` +
        "It is rendered by PinController.panel_info via pin.panel/property_records/, which self-polls client-side for up to " +
        "MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS (60s) per page load before going quiet - reloading the page restarts that budget, which is what this " +
        "wait does. Check, in order: LocationCache for this Location and cache_source='property_records'; a celery worker consuming the panel_fetch " +
        "queue (celery-worker-panels); UL_ALLOW_OUTBOUND_APIS and REData configuration on this deployment."
    );
}

/** Reloads the pin page until the Property Records card leaves its loading state, or the wait runs out. */
async function waitForPropertyRecordsCard(page: Page, pinSlug: string, tags: MetricTags): Promise<boolean> {
    const startedAt = Date.now();
    const ready = await waitForOrNull(
        async () => {
            await openPrivatePin(page, pinSlug, { metricPrefix: null });
            return page
                .locator(CARD_SELECTOR)
                .waitFor({ state: "visible", timeout: 65_000 })
                .then(() => true)
                .catch(() => false);
        },
        (visible) => visible === true,
        {
            what: `the Property Records card on ${pinDetail(pinSlug)} to leave its loading state`,
            timeoutMs: CARD_WAIT_TIMEOUT_MS,
            intervalMs: CARD_RELOAD_GAP_MS,
            describe: (visible) => (visible ? "visible" : "still hidden/pending"),
        },
    );
    if (ready) {
        recordMetric({ name: "hrsh.ownership.seconds_to_card_ready", value: Math.round((Date.now() - startedAt) / 1000), unit: "s", tags });
    }
    recordMetric({ name: "hrsh.ownership.card_ready", value: ready ? 1 : 0, unit: "count", tags });
    return ready === true;
}

/** Reads the card as currently rendered. Only meaningful once {@link waitForPropertyRecordsCard} has returned true. */
async function readPropertyRecordsCard(page: Page): Promise<PropertyRecordsSnapshot> {
    const nameLocator = page.locator(OWNER_NAME_SELECTOR).first();
    const ownerName = (await nameLocator.count()) > 0 ? ((await nameLocator.textContent()) ?? "").trim() || null : null;
    const chips = (await page.locator(CHIP_SELECTOR).allTextContents()).map((chip) => chip.trim());
    return { ownerName, chips };
}

test.describe("Hudson River State Hospital - Property Records ownership gate (private pin page)", () => {
    test("provisioning gives the primary account no property_owners feature and the subscriber account exactly that feature", async () => {
        const primary = requireAccount(PRIMARY_ROLE);
        const subscriber = requireAccount(SUBSCRIBER_ROLE);

        expect(
            hasFeature(primary, PROPERTY_OWNERS_FEATURE),
            `the "${PRIMARY_ROLE}" account holds ${PROPERTY_OWNERS_FEATURE} (features: ${primary.features.join(", ")}), so it cannot serve as the ` +
                "non-subscriber side of this test. Provision it without --subscriber-roles.",
        ).toBe(false);
        expect(
            hasFeature(subscriber, PROPERTY_OWNERS_FEATURE),
            `the "${SUBSCRIBER_ROLE}" account does not hold ${PROPERTY_OWNERS_FEATURE} (features: ${subscriber.features.join(", ") || "none"}). Re-run ` +
                `"manage.py provision_integration_env" with --subscriber-roles ${SUBSCRIBER_ROLE}.`,
        ).toBe(true);
    });

    test("the Property Records card eventually loads on the non-subscriber's private pin page", async ({ campus, page }) => {
        const ready = await waitForPropertyRecordsCard(page, campus.pin.slug, { subscriber: false });
        expect(ready, cardDiagnosis(campus.pin.slug)).toBe(true);
    });

    test("the Property Records card eventually loads on the subscriber's own private pin page", async ({ subscriberApi, subscriberPage }) => {
        const pin = await findOrCreateSubscriberPin(subscriberApi);
        const ready = await waitForPropertyRecordsCard(subscriberPage, pin.slug, { subscriber: true });
        expect(ready, cardDiagnosis(pin.slug)).toBe(true);
    });

    test("an official owner is on record, and the subscriber's card shows the owner's name", async ({ subscriberApi, subscriberPage }, testInfo) => {
        const pin = await findOrCreateSubscriberPin(subscriberApi);
        const ready = await waitForPropertyRecordsCard(subscriberPage, pin.slug, { subscriber: true });
        expect(ready, cardDiagnosis(pin.slug)).toBe(true);

        const card = await readPropertyRecordsCard(subscriberPage);
        expect(
            card.ownerName,
            "the subscriber's Property Records card shows no owner name. The requirement presumes an official owner is on record for " +
                "this parcel; if REData genuinely has none, that is a data gap to raise, not an application defect this test can tell apart " +
                "from one from here",
        ).toBeTruthy();

        const matchesKnownCandidate = KNOWN_OWNER_CANDIDATES.some((candidate) => (card.ownerName ?? "").toLowerCase().includes(candidate.toLowerCase()));
        await testInfo.attach("owner-name.txt", {
            body: `Card shows: ${JSON.stringify(card.ownerName)}\nMatches a known candidate (${KNOWN_OWNER_CANDIDATES.join(", ")}): ${matchesKnownCandidate}`,
            contentType: "text/plain",
        });
    });

    test("the non-subscriber never sees the owner's name anywhere on the page, and sees the locked chip instead", async ({ campus, page, subscriberApi, subscriberPage }) => {
        const subscriberPin = await findOrCreateSubscriberPin(subscriberApi);
        expect(await waitForPropertyRecordsCard(subscriberPage, subscriberPin.slug, { subscriber: true }), cardDiagnosis(subscriberPin.slug)).toBe(true);
        const subscriberCard = await readPropertyRecordsCard(subscriberPage);
        test.skip(!subscriberCard.ownerName, "no owner name on the subscriber's card to check for a leak - see the previous test, which reports that as the finding.");

        expect(await waitForPropertyRecordsCard(page, campus.pin.slug, { subscriber: false }), cardDiagnosis(campus.pin.slug)).toBe(true);
        const pageText = (await page.locator("body").innerText()).toLowerCase();

        expect(
            pageText.includes((subscriberCard.ownerName ?? "").toLowerCase()),
            `the non-subscriber's page contains "${subscriberCard.ownerName}" (the name shown to the subscriber) somewhere on the page, not just ` +
                "outside the gated card. can_see_official_owners must be false for this account for the whole page to be clean, not merely for the " +
                "heading it controls directly",
        ).toBe(false);

        const nonSubscriberCard = await readPropertyRecordsCard(page);
        expect(
            nonSubscriberCard.chips,
            `the non-subscriber's card shows no "${LOCKED_CHIP_TEXT}" chip even though an owner is on record. A non-subscriber must be told the ` +
                "record exists and is withheld, not shown a card that simply omits the heading",
        ).toContain(LOCKED_CHIP_TEXT);
        expect(nonSubscriberCard.ownerName, "the non-subscriber's own card heading carries an owner name; can_see_official_owners should have suppressed it").toBeNull();
    });

    test("the ungated parcel facts are present, and identical, for both accounts", async ({ campus, page, subscriberApi, subscriberPage }) => {
        const subscriberPin = await findOrCreateSubscriberPin(subscriberApi);
        expect(await waitForPropertyRecordsCard(subscriberPage, subscriberPin.slug, { subscriber: true }), cardDiagnosis(subscriberPin.slug)).toBe(true);
        expect(await waitForPropertyRecordsCard(page, campus.pin.slug, { subscriber: false }), cardDiagnosis(campus.pin.slug)).toBe(true);

        const subscriberMeta = await subscriberPage.locator(META_SELECTOR).allTextContents();
        const nonSubscriberMeta = await page.locator(META_SELECTOR).allTextContents();

        expect(subscriberMeta.length, "the subscriber's card has no ungated parcel facts (address, APN, ...) at all - the gate should withhold only the owner identity").toBeGreaterThan(0);
        expect(
            nonSubscriberMeta,
            `the ungated facts differ between accounts: subscriber sees ${JSON.stringify(subscriberMeta)}, non-subscriber sees ${JSON.stringify(nonSubscriberMeta)}. Only the owner ` +
                "identity is meant to depend on the subscription; parcel facts come from the same REData record for both",
        ).toEqual(subscriberMeta);
    });
});
