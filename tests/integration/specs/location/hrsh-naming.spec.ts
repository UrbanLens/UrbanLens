/**
 * The same property from two points: the requirement pin and the courtyard point Jess pinned on k3s-staging.
 *
 * Staging drew a circle at the courtyard, titled its wiki "Courtyard Drive" (a service road), aliased the parcel with
 * the nearest CRIS building's name, found no Wikipedia article, and showed a single building in the CRIS panel. Every
 * test here runs at both points and asserts the fixed behaviour exactly: the county parcel, the National Register
 * title the naming metric picks (D20), register and Wikipedia aliases with no
 * building or road names, the campus buildings as building child pins, the Wikipedia link and article, and the CRIS
 * campus roster.
 */

import {
    childPins,
    expect,
    locationDataTest as test,
    readPin,
    skipUnlessLocationDataEnabled,
    waitForChildPins,
    waitForWiki,
    type CampusFixture,
    type SyncPinRow,
    type WikiDetail,
} from "./fixtures.js";
import {
    approximateAreaSqm,
    BLDG45_NAME,
    containsCoordinate,
    COURTYARD_ROAD,
    EXPECTED_PARCEL_AREA_SQM,
    MIN_CAMPUS_BUILDINGS,
    NRHP_TITLE,
    WIKIPEDIA_TITLE,
    WIKIPEDIA_URL_FRAGMENT,
} from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** Naming, the building sweep and the register/CRIS panels all land in the background after the page visit. */
const ENRICHMENT_WAIT_MS = 420_000;
const POLL_MS = 15_000;

/** A CRIS building's name: "BLDG 45/MORTUARY & LAB (1896)". */
const CRIS_BUILDING_NAME = /^BLDG\s+\d+/i;

interface Alias {
    name?: string;
    kind?: string;
}

interface PinWithAliases {
    aliases?: Alias[];
}

interface Link {
    url?: string;
    name?: string;
}

interface CrisPanel {
    info?: { heading_name?: string; meta?: Array<{ label?: string; value?: string }> } | null;
}

const SITES = ["campus", "courtyard"] as const;
type SiteName = (typeof SITES)[number];

function pick(site: SiteName, fixtures: { campus: CampusFixture; courtyard: CampusFixture }): CampusFixture {
    return fixtures[site];
}

function names(aliases: Alias[] | undefined): string[] {
    return (aliases ?? []).map((alias) => alias.name ?? "");
}

async function wikiOf(fixture: CampusFixture): Promise<WikiDetail | null> {
    return waitForWiki(fixture.api, fixture.pin.location_slug);
}

async function buildingChildren(fixture: CampusFixture): Promise<SyncPinRow[]> {
    return (await childPins(fixture)).filter((child) => child.pin_type === "building");
}

async function listOf<T>(fixture: CampusFixture, path: string): Promise<T[]> {
    const body = await fixture.api.json<T[] | { results?: T[] }>("get", path);
    return Array.isArray(body) ? body : (body.results ?? []);
}

for (const siteName of SITES) {
    test.describe(`Hudson River State Hospital from the ${siteName} pin`, () => {
        test("the pin stands on the county parcel, not the fallback circle", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            const pin = await readPin(fixture.api, fixture.pin.slug);
            const boundary = pin.boundary;
            expect(boundary, `${siteName}: the pin has no boundary`).not.toBeNull();
            expect(["Polygon", "MultiPolygon"], `${siteName}: the boundary is a ${boundary!.type}`).toContain(boundary!.type);
            const area = approximateAreaSqm(boundary);
            expect(
                area,
                `${siteName}: the boundary is about ${Math.round(area).toLocaleString()} m². The 50 m fallback circle is ~7,850 m²; the tax ` +
                    "parcel (3532 North Rd) is ~473,000 m²",
            ).toBeGreaterThan(EXPECTED_PARCEL_AREA_SQM.min);
            expect(area).toBeLessThan(EXPECTED_PARCEL_AREA_SQM.max);
            expect(containsCoordinate(boundary, fixture.site.point), `${siteName}: the boundary does not contain the pinned point`).toBe(true);
        });

        test("both points share one property and one wiki", async ({ campus, courtyard }) => {
            campus.requireBoundary();
            courtyard.requireBoundary();
            const [campusWiki, courtyardWiki] = await Promise.all([wikiOf(campus), wikiOf(courtyard)]);
            expect(campusWiki && courtyardWiki, "one of the two pins has no wiki").toBeTruthy();
            expect(
                courtyardWiki!.uuid,
                "the courtyard pin reads a different wiki from the requirement pin, so the two points resolved to different properties",
            ).toBe(campusWiki!.uuid);
        });

        test("the wiki is titled by its Wikipedia article, above the National Register listing", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            const wiki = await waitForOrNull(() => wikiOf(fixture), (value) => value?.name === WIKIPEDIA_TITLE, {
                what: `the ${siteName} wiki titled "${WIKIPEDIA_TITLE}"`,
                timeoutMs: ENRICHMENT_WAIT_MS,
                intervalMs: POLL_MS,
                describe: (value) => `name=${JSON.stringify(value?.name ?? null)}`,
            });
            const last = wiki ?? (await wikiOf(fixture));
            expect(
                last?.name,
                `${siteName}: the name-tier metric ranks the matched Wikipedia article first (D20, services/locations/name_tiers.py): ` +
                    `"${NRHP_TITLE}" names one building on the plot, and "${COURTYARD_ROAD}" is a road and must never win`,
            ).toBe(WIKIPEDIA_TITLE);
        });

        test("the aliases carry the register and Wikipedia titles, and no building or road names", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            const wiki = await waitForOrNull(
                () => wikiOf(fixture),
                (value) => names(value?.aliases).includes(NRHP_TITLE) && names(value?.aliases).includes(WIKIPEDIA_TITLE),
                {
                    what: `the register and Wikipedia titles among the ${siteName} wiki's aliases`,
                    timeoutMs: ENRICHMENT_WAIT_MS,
                    intervalMs: POLL_MS,
                    describe: (value) => JSON.stringify(names(value?.aliases)),
                },
            );
            const wikiAliases = names((wiki ?? (await wikiOf(fixture)))?.aliases);
            expect(wikiAliases, `${siteName}: wiki aliases`).toContain(NRHP_TITLE);
            expect(wikiAliases, `${siteName}: wiki aliases`).toContain(WIKIPEDIA_TITLE);

            const pinAliases = names((await fixture.api.json<PinWithAliases>("get", `pins/${fixture.pin.slug}/`)).aliases);
            expect(pinAliases, `${siteName}: pin aliases`).toContain(NRHP_TITLE);
            expect(pinAliases, `${siteName}: pin aliases`).toContain(WIKIPEDIA_TITLE);

            const buildingNames = new Set((await buildingChildren(fixture)).map((child) => child.name).filter(Boolean));
            for (const [where, aliases] of [
                ["wiki", wikiAliases],
                ["pin", pinAliases],
            ] as const) {
                const offending = aliases.filter((alias) => CRIS_BUILDING_NAME.test(alias) || buildingNames.has(alias) || alias === COURTYARD_ROAD);
                expect(
                    offending,
                    `${siteName}: the parcel ${where} carries building or road names as aliases. A campus of many buildings lends none of ` +
                        "their names to the parcel; each is its own building pin",
                ).toEqual([]);
            }
        });

        test("the campus buildings are building child pins, BLDG 45 among them", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            await waitForChildPins(fixture, { min: MIN_CAMPUS_BUILDINGS, timeoutMs: ENRICHMENT_WAIT_MS });
            const buildings = await waitForOrNull(() => buildingChildren(fixture), (list) => list.some((child) => child.name === BLDG45_NAME), {
                what: `a building child pin named "${BLDG45_NAME}" under the ${siteName} pin`,
                timeoutMs: ENRICHMENT_WAIT_MS,
                intervalMs: POLL_MS,
                describe: (list) => `${list.length} building child pin(s)`,
            });
            const last = buildings ?? (await buildingChildren(fixture));
            expect(last.length, `${siteName}: building child pins`).toBeGreaterThanOrEqual(MIN_CAMPUS_BUILDINGS);
            expect(
                last.map((child) => child.name),
                `${siteName}: "${BLDG45_NAME}" is a CRIS building on the parcel with no OSM footprint; it should be its own building pin`,
            ).toContain(BLDG45_NAME);
        });

        test("the Wikipedia article is linked and seeds the pin and wiki articles", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            const links = await waitForOrNull(
                () => listOf<Link>(fixture, `pins/${fixture.pin.slug}/links/`),
                (list) => list.some((link) => (link.url ?? "").includes(WIKIPEDIA_URL_FRAGMENT)),
                {
                    what: `the Wikipedia link on the ${siteName} pin`,
                    timeoutMs: ENRICHMENT_WAIT_MS,
                    intervalMs: POLL_MS,
                    describe: (list) => JSON.stringify(list.map((link) => link.url)),
                },
            );
            expect(links, `${siteName}: the pin never gained a link to ${WIKIPEDIA_URL_FRAGMENT}`).not.toBeNull();
            const wikiLinks = await listOf<Link>(fixture, `wikis/${fixture.pin.location_slug}/links/`);
            expect(wikiLinks.map((link) => link.url ?? "").some((url) => url.includes(WIKIPEDIA_URL_FRAGMENT)), `${siteName}: the wiki has no Wikipedia link`).toBe(true);

            for (const path of [`pins/${fixture.pin.slug}/article/`, `wikis/${fixture.pin.location_slug}/article/`]) {
                const article = await waitForOrNull(() => fixture.api.get(path), (response) => response.status() === 200, {
                    what: `an article at ${path}`,
                    timeoutMs: ENRICHMENT_WAIT_MS,
                    intervalMs: POLL_MS,
                    describe: (response) => `HTTP ${response.status()}`,
                });
                expect(article, `${siteName}: nothing was seeded at ${path}`).not.toBeNull();
                const content = ((await fixture.api.json<{ content?: string }>("get", path)).content ?? "").toLowerCase();
                expect(content, `${siteName}: ${path} is not the Hudson River State Hospital article`).toContain("hudson river state hospital");
                expect(content, `${siteName}: ${path} carries no Wikipedia attribution`).toContain("wikipedia");
            }
        });

        test("the CRIS panel shows the campus listing and its roster, not one building", async ({ campus, courtyard }) => {
            const fixture = pick(siteName, { campus, courtyard });
            fixture.requireBoundary();
            const panel = await waitForOrNull(
                async () => {
                    const response = await fixture.api.get(`pins/${fixture.pin.slug}/panels/cris_building/`);
                    return response.status() === 200 ? ((await response.json()) as CrisPanel) : null;
                },
                (value) => value?.info?.heading_name === NRHP_TITLE,
                {
                    what: `the ${siteName} pin's CRIS card headed by the National Register listing`,
                    timeoutMs: ENRICHMENT_WAIT_MS,
                    intervalMs: POLL_MS,
                    describe: (value) => `heading=${JSON.stringify(value?.info?.heading_name ?? null)}`,
                },
            );
            expect(panel?.info?.heading_name, `${siteName}: a site-scope pin's CRIS card names the site, never "${BLDG45_NAME}"`).toBe(NRHP_TITLE);
            const roster = (panel?.info?.meta ?? []).find((item) => item.label === "Surveyed buildings")?.value ?? "";
            expect(roster.split(";").length, `${siteName}: the card names ${JSON.stringify(roster)} as the campus's surveyed buildings`).toBeGreaterThan(1);
        });
    });
}
