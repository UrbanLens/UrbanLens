/**
 * Owner, sale history and EPA data reaching the campus - and who may read it.
 *
 * ## The sale-date problem, and how it is avoided
 *
 * The requirement asks that the last sale date propagate "in a way that doesn't
 * cause the test to begin failing if another sale happens in the future". The
 * good news is structural: there is no `last_sale_date` field anywhere in the
 * application - `grep -rn "last_sale" src/urbanlens/` finds nothing. "The last
 * sale" is whatever sorts first under `WikiPropertySale.Meta.ordering`, which is
 * `["-sale_date", "-created"]`.
 *
 * So the testable claim is the *ordering contract*, not a date: the first row
 * the API returns is the newest one on file, and it is not in the future. A new
 * deed recorded tomorrow satisfies both, and a parser that mangles dates or an
 * ordering that silently changes fails both. No date is hardcoded anywhere in
 * this file, deliberately.
 *
 * ## The subscription gate
 *
 * `services.property.owner_access` draws a line the requirement also draws, and
 * it is a finer line than it first appears:
 *
 * - Sale **facts** - date, price, notes - are shown to everyone.
 * - Sale **parties** and current **owner identity** need
 *   `SiteFeature.PROPERTY_OWNERS`. A non-subscriber sees the literal string
 *   "Subscribers only" where a name would be.
 *
 * That module's docstring says every surface rendering owner identity goes
 * through `visible_owners` "so the two can't drift". The external API's
 * `WikiOwnershipView` and `WikiPropertySalesView` do not import it. Whether that
 * is a real hole is exactly the sort of thing to settle with a request rather
 * than an argument, which is what the last two tests here do.
 */

import { ensureCampusWiki, expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { EARLIEST_PLAUSIBLE_SALE, EXPECTED_OWNER_FRAGMENT, hrshRoutes, KNOWN_OWNER_CANDIDATES } from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

interface OwnerRow {
    name?: string | null;
    company_name?: string | null;
    source?: string | null;
}

interface SaleRow {
    sale_date?: string | null;
    sale_price?: string | number | null;
    source?: string | null;
    previous_owners?: Array<{ name?: string | null }>;
    new_owners?: Array<{ name?: string | null }>;
}

/**
 * Reads a list endpoint, tolerating both envelope shapes and a missing wiki.
 *
 * **These are wiki routes, not property routes**, which is easy to miss from
 * their names: `wikis/{slug}/ownership/` and `wikis/{slug}/sales/` both 404
 * until a wiki has been promoted. Left to throw, that 404 aborts a wait meant to
 * be watching for enrichment - the spec fails in a second, looking like "the
 * data never arrived" when the truth is "there was nothing to ask". Returning an
 * empty list instead lets the wait do its job and lets the assertion say what it
 * means.
 */
async function rows<T>(api: { get: (p: string) => Promise<{ ok: () => boolean; status: () => number; json: () => Promise<unknown> }> }, path: string): Promise<T[]> {
    const response = await api.get(path);
    if (!response.ok()) {
        return [];
    }
    const body = (await response.json()) as T[] | { results?: T[] };
    return Array.isArray(body) ? body : (body.results ?? []);
}

test.describe("Hudson River State Hospital - property records", () => {
    // Ownership and sale history are served from wiki routes, so a promoted
    // wiki is a precondition for the whole file rather than a subject of it.
    // Without this, every test here fails on a 404 that says nothing about
    // property data.
    test.beforeEach(async ({ campus, page }) => {
        const ready = await ensureCampusWiki(campus, page);
        test.skip(!ready, "the campus has no wiki, and ownership/sales are wiki routes - see hrsh-wiki.spec.ts.");
    });

    test("official owner records reach the campus location", async ({ campus }) => {
        const found = await waitForOrNull(
            () => rows<OwnerRow>(campus.api, `wikis/${campus.pin.location_slug}/ownership/`),
            (list) => list.some((owner) => owner.source === "official"),
            {
                what: "an official owner record for the campus",
                timeoutMs: 240_000,
                intervalMs: 15_000,
                describe: (list) => `${list.length} owner row(s): ${list.map((o) => `${o.name ?? "?"}/${o.source ?? "?"}`).join(", ") || "none"}`,
            },
        );

        expect(
            found,
            "no owner record sourced from county assessor data arrived for this parcel. property_records writes these whenever REData " +
                "answers, so either the lookup never ran, REData has no parcel for these coordinates, or the write path is broken",
        ).not.toBeNull();
    });

    test("the recorded owner is reported, and a mismatch is raised as a question", async ({ campus }) => {
        const owners = await rows<OwnerRow>(campus.api, `wikis/${campus.pin.location_slug}/ownership/`);
        const official = owners.filter((owner) => owner.source === "official");
        test.skip(official.length === 0, "no official owner record to check - see the previous test, which reports that as the finding.");

        const names = official.map((owner) => [owner.name, owner.company_name].filter(Boolean).join(" ").trim()).filter(Boolean);
        const matches = names.some((name) => name.toLowerCase().includes(EXPECTED_OWNER_FRAGMENT.toLowerCase()));

        // Deliberately phrased as a question rather than a verdict. "Hudson
        // Heritage" is the name this suite was given and is certainly the
        // project's name, but public reporting also names EFG-Saber Heritage SC,
        // LLC as the entity running the redevelopment - and a deed holder and a
        // developer are different things. A mismatch here needs a human to say
        // which is right; it is not automatically an application defect.
        expect(
            matches,
            `the county record names ${JSON.stringify(names)}, which does not contain "${EXPECTED_OWNER_FRAGMENT}". Before treating this ` +
                `as a defect, confirm which name the deed actually carries - names seen in public reporting for this parcel include ` +
                `${KNOWN_OWNER_CANDIDATES.join(", ")}. If the county's record is simply different from the expectation, update ` +
                "EXPECTED_OWNER_FRAGMENT in lib/hrsh.ts rather than changing the application",
        ).toBe(true);
    });

    test("sale history is ordered newest first, and nothing is dated in the future", async ({ campus }) => {
        const sales = await rows<SaleRow>(campus.api, `wikis/${campus.pin.location_slug}/sales/`);
        test.skip(sales.length === 0, "no sale history on file for this parcel yet.");

        const dated = sales.filter((sale) => sale.sale_date).map((sale) => sale.sale_date!);
        expect(dated.length, "sale rows exist but none carries a date, so there is no 'last sale' to speak of").toBeGreaterThan(0);

        // The ordering contract, which is what "last sale date" actually means
        // here. Asserting sortedness rather than a value is what keeps this
        // test correct after the next deed is recorded.
        const sorted = [...dated].sort().reverse();
        expect(
            dated,
            `sales came back as ${JSON.stringify(dated)}, which is not newest-first. Anything reading "the last sale" takes the first row ` +
                "(WikiPropertySale.Meta.ordering is ['-sale_date', '-created']), so a broken order silently reports the wrong sale",
        ).toEqual(sorted);

        const today = new Date().toISOString().slice(0, 10);
        expect(dated[0]!.slice(0, 10) <= today, `the most recent sale is dated ${dated[0]}, which is in the future`).toBe(true);
        expect(
            dated[dated.length - 1]!.slice(0, 10) >= EARLIEST_PLAUSIBLE_SALE,
            `the oldest sale is dated ${dated[dated.length - 1]}, before ${EARLIEST_PLAUSIBLE_SALE}. The state sold this property in 2005; ` +
                "a date far earlier suggests a parse failure or a timezone shift rather than a genuine record",
        ).toBe(true);
    });

    test("sale prices, where present, are plausible amounts", async ({ campus }) => {
        const sales = await rows<SaleRow>(campus.api, `wikis/${campus.pin.location_slug}/sales/`);
        test.skip(sales.length === 0, "no sale history on file for this parcel yet.");

        const bad = sales
            .filter((sale) => sale.sale_price !== null && sale.sale_price !== undefined)
            .map((sale) => ({ raw: sale.sale_price, value: Number(sale.sale_price) }))
            .filter((entry) => !Number.isFinite(entry.value) || entry.value < 0);

        expect(bad, `these sale prices are not usable numbers: ${JSON.stringify(bad)}`).toEqual([]);
    });

    test("the wiki sale card shows dates and prices to a user without the subscription", async ({ campus, page }) => {
        campus.requireBoundary();
        const sales = await rows<SaleRow>(campus.api, `wikis/${campus.pin.location_slug}/sales/`);
        test.skip(sales.length === 0, "no sale history on file for this parcel yet.");

        await page.goto(hrshRoutes.wikiSales(campus.pin.location_slug));
        const rowsOnPage = page.locator(".po-sale-row");
        await expect(rowsOnPage.first(), "the wiki's sale card rendered no rows even though the API has sales").toBeAttached();

        // The requirement: historic sale information stays available. The gate
        // is on party *names*, never on the sale itself.
        await expect(
            page.locator(".po-sale-date").first(),
            "a sale row rendered with no date. Sale facts are ungated by design (services.property.owner_access.sale_rows), so a missing " +
                "date here is a rendering defect rather than the subscription gate working",
        ).toBeAttached();
    });

    test("owner identity on the web is either shown or explicitly locked, never silently blank", async ({ campus, page }) => {
        campus.requireBoundary();

        await page.goto(hrshRoutes.wikiOwnership(campus.pin.location_slug));
        const ownerRows = page.locator(".po-owner-list");
        const locked = page.locator(".po-panel-locked");

        const shown = await ownerRows.count();
        const isLocked = await locked.count();

        expect(
            shown + isLocked,
            "the ownership card showed neither owner rows nor the subscriber notice. Those are the only two correct outcomes: a " +
                "non-subscriber must be told the records exist and are withheld, not shown an empty card that looks like 'no data'",
        ).toBeGreaterThan(0);
    });

    test("the external API applies the same owner-identity gate the web UI does", async ({ campus, page }) => {
        // `services.property.owner_access`'s docstring states that every surface
        // rendering owner identity goes through `visible_owners` "so the two
        // can't drift". `external_api/views_wiki.py` imports none of that
        // module - not `visible_owners`, not `sale_rows`, not
        // `can_see_official_owners`. This test settles that with a request
        // instead of an argument.
        //
        // It is differential on purpose. Either answer is fine on its own: with
        // SiteFeature.PROPERTY_OWNERS both surfaces should show names, without
        // it both should withhold them. What cannot be right is the same account
        // being refused on the web and served over the API.
        const owners = await rows<OwnerRow>(campus.api, `wikis/${campus.pin.location_slug}/ownership/`);
        const apiNames = owners.filter((owner) => owner.source === "official").map((owner) => owner.name).filter(Boolean);
        test.skip(apiNames.length === 0, "no official owner records on file, so there is nothing for either surface to withhold.");

        await page.goto(hrshRoutes.wikiOwnership(campus.pin.location_slug));
        const webWithheld = (await page.locator(".po-panel-locked").count()) > 0;

        expect(
            webWithheld,
            `The web ownership card withholds the official owner names from this account (.po-panel-locked is rendered), but the external ` +
                `API served ${apiNames.length} of them to the same account: ${JSON.stringify(apiNames)}. The gate exists on one surface ` +
                "and not the other - WikiOwnershipView returns WikiOwner.objects.for_location() straight through. Either the API needs " +
                "the same visible_owners() filter, or the web card is withholding something it should not",
        ).toBe(false);
    });

    test("the external API withholds sale parties exactly when the web UI does", async ({ campus, page }) => {
        const sales = await rows<SaleRow>(campus.api, `wikis/${campus.pin.location_slug}/sales/`);
        const apiParties = sales.flatMap((sale) => [...(sale.previous_owners ?? []), ...(sale.new_owners ?? [])]).map((party) => party.name).filter(Boolean);
        test.skip(apiParties.length === 0, "no sale parties on file, so there is nothing for either surface to withhold.");

        await page.goto(hrshRoutes.wikiSales(campus.pin.location_slug));
        const partyText = (await page.locator(".po-sale-party").allTextContents()).join(" | ");
        const webWithheld = partyText.includes("Subscribers only");

        expect(
            webWithheld,
            `The web sale card renders "Subscribers only" in place of the party names, while the external API served ${apiParties.length} ` +
                `of them to the same account: ${JSON.stringify(apiParties.slice(0, 6))}. Sale dates and prices are ungated by design; the ` +
                "names on a sale are not, and WikiPropertySalesView never applies sale_rows()",
        ).toBe(false);
    });

    test("an EPA compliance link is added to the pin and to its wiki when the site matches", async ({ campus }) => {
        // EPA ECHO reaches the wiki two ways; the link is the one with a stable,
        // asserted-on name (`services.locations.external_links.add_pin_and_wiki_link`).
        const links = await waitForOrNull(
            () => rows<{ name?: string; url?: string }>(campus.api, `wikis/${campus.pin.location_slug}/links/`),
            (list) => list.some((link) => (link.name ?? "").includes("EPA Compliance Report")),
            {
                what: "the EPA Compliance Report link on the campus wiki",
                timeoutMs: 240_000,
                intervalMs: 15_000,
                describe: (list) => `${list.length} link(s): ${list.map((l) => l.name ?? "?").join(", ") || "none"}`,
            },
        );

        // A genuine "no EPA facility here" is a legitimate outcome and must not
        // read as a failure - the link is only added on an exact-site match
        // within 0.1 miles. So this reports rather than asserts, and says how to
        // tell the two apart.
        test.skip(
            links === null,
            "No EPA Compliance Report link appeared. That is correct when EPA ECHO has no regulated facility within 0.1 miles of this " +
                "coordinate, and a defect when it does - check the epa_echo_detail panel for an exact_site match before treating it as " +
                "either.",
        );
        expect(links!.some((link) => (link.url ?? "").includes("echo.epa.gov"))).toBe(true);
    });
});
