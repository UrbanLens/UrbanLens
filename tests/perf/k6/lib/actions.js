/**
 * What the noisy neighbour does. One export per phase action in `schedule.js`.
 *
 * Each of these is a thing a single ordinary user can do from the interface,
 * with no special permission and no API key - which is the whole point. The
 * question this suite asks is not "can the site be overloaded" but "can one
 * account's ordinary use degrade another account's", and an action that needed
 * privileges would not answer it.
 *
 * Every action returns its response so the caller can decide what counts as
 * success. None of them assert: a 500 under load is a result, not a harness
 * failure, and a harness that aborted on the first one would destroy the
 * measurement it exists to take.
 */

import { postForm, postJson, get } from "./session.js";

const MAP_INIT = "/dashboard/map/init/";
const MAP_SEARCH = "/dashboard/map/search/";
const IMPORT_CONFIRMED = "/dashboard/map/pin/import/confirmed/";

/** Degrees between imported pins.
 *
 * Half a degree, not the smallest value the unique constraint permits: the
 * importer matches a new pin to an existing `Location` by proximity, so pins a
 * few metres apart collapse into one row and an import of N creates far fewer
 * than N. Measured on the live endpoint - three pins 0.0001 degrees apart
 * produced a single row.
 */
const IMPORT_STEP = 0.5;

/** Where imported pins go. Deliberately far from the seeded block, so an import
 * cannot merge into seeded locations and quietly do less work than it claims.
 */
const IMPORT_ORIGIN = { lat: 10.0, lng: -150.0 };

/** Columns in the import grid.
 *
 * Wide on purpose. The import phase loops for its whole duration, and each
 * iteration continues where the last stopped, so a narrow grid climbs in
 * latitude fast: at 23 columns and 500 pins an iteration, the fourteenth import
 * would be asking the server to create pins north of the pole. 600 columns at
 * half a degree spans nearly the whole usable longitude range and advances
 * latitude by well under a degree per import.
 */
const IMPORT_COLUMNS = 600;

/**
 * Serialise the whole account into one HTML document.
 *
 * No cap and no cache on this path, so its cost is linear in the account and
 * paid inside the request. This is the cheapest way for one user to hold a
 * worker for a long time.
 */
export function mapInit(session, fixtures, tags) {
    return get(session, MAP_INIT, Object.assign({ endpoint: "map_init" }, tags), { timeout: fixtures.actorTimeout });
}

/**
 * Post a filter and get every matching pin back as HTML.
 *
 * `name` is set to a prefix every seeded pin shares, so the filter genuinely
 * runs and genuinely matches everything - a filter that matched nothing would
 * measure the query planner rather than the response builder.
 */
export function mapSearch(session, fixtures, tags) {
    return postForm(
        session,
        MAP_SEARCH,
        { name: fixtures.pinNamePrefix },
        Object.assign({ endpoint: "map_search" }, tags),
        { timeout: fixtures.actorTimeout },
    );
}

/**
 * Rename nothing and change one colour on the label every pin carries.
 *
 * The colour rather than the description on purpose. `LabelEditView.post` runs
 * `Pin.objects.filter(labels=label).update(updated=now())` unconditionally, so
 * today any POST at all costs one row-write per carrying pin - but colour is a
 * field the pin payload actually contains, so if change detection is ever added
 * this stays an edit that genuinely has to fan out. A description-only edit
 * would silently become free and this phase would stop testing anything.
 *
 * The name is posted unchanged because the seeder finds its label by name; a
 * run that renamed it would leave the next seed creating a second one.
 */
export function labelEdit(session, fixtures, tags) {
    const colors = ["#b34747", "#4747b3"];
    const path = `/dashboard/${fixtures.labelUrlKind}/${fixtures.labelId}/edit/`;
    return postForm(
        session,
        path,
        {
            name: fixtures.labelName,
            color: colors[__ITER % colors.length],
            icon: "",
            description: "",
            order: "0",
        },
        Object.assign({ endpoint: "label_edit" }, tags),
        { timeout: fixtures.actorTimeout },
    );
}

/**
 * Import a batch of pins through the confirmed-import SSE endpoint.
 *
 * Runs entirely inside the request: a generator writing rows and streaming
 * progress, with no task boundary anywhere in it (P96). The response is a
 * `text/event-stream` that stays open until the last pin is written, so k6 sits
 * on the connection for the whole import - which is exactly what a real
 * browser doing this does, and exactly why it is worth measuring.
 *
 * `auto_tag` is off so the run measures the import rather than the AI queue it
 * would otherwise fill.
 */
export function importConfirmed(session, fixtures, tags) {
    const pins = [];
    // Offset by iteration so a second import in the same run does not re-import
    // the same coordinates and get absorbed as matches against the first.
    const base = __ITER * fixtures.importPins;
    for (let index = 0; index < fixtures.importPins; index += 1) {
        const n = base + index;
        pins.push({
            name: `Perf Import ${n}`,
            lat: IMPORT_ORIGIN.lat + Math.floor(n / IMPORT_COLUMNS) * IMPORT_STEP,
            lng: IMPORT_ORIGIN.lng + (n % IMPORT_COLUMNS) * IMPORT_STEP,
            description: "",
            cid: "",
            maps_url: "",
            label_ids: [],
        });
    }

    return postJson(
        session,
        IMPORT_CONFIRMED,
        {
            auto_tag: false,
            lists: [{ stem: "Perf Import", create_category: false, label_ids: [], pins }],
        },
        Object.assign({ endpoint: "import_confirmed" }, tags),
        // k6's default request timeout is 60s and this legitimately runs for
        // minutes, so without this the import is cut off by the harness and
        // recorded as an error the server never made.
        { timeout: fixtures.importTimeout },
    );
}

/** Every action by the name `schedule.js` refers to it by. */
export const ACTIONS = { mapInit, mapSearch, labelEdit, importConfirmed };
