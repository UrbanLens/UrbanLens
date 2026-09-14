/** What the noisy neighbour does. Each of these is a thing a single ordinary user can do from the interface, with no special permission and no API key - which is the whole point. */

import { sleep } from "k6";

import { postForm, postJson, get } from "./session.js";

const MAP_INIT = "/dashboard/map/init/";
const MAP_SEARCH = "/dashboard/map/search/";
const IMPORT_CONFIRMED = "/dashboard/map/pin/import/confirmed/";

/** Degrees between imported pins. */
const IMPORT_STEP = 0.5;

/** Where imported pins go. Deliberately far from the seeded block, so an import
 * cannot merge into seeded locations and quietly do less work than it claims.
 */
const IMPORT_ORIGIN = { lat: 10.0, lng: -150.0 };

/** Columns in the import grid. Wide on purpose. */
const IMPORT_COLUMNS = 600;

/** Serialise the whole account into one HTML document. No cap and no cache on this path, so its cost is linear in the account and paid inside the request. */
export function mapInit(session, fixtures, tags) {
    return get(session, MAP_INIT, Object.assign({ endpoint: "map_init" }, tags), { timeout: fixtures.actorTimeout });
}

/** Post a filter and get every matching pin back as HTML. */
export function mapSearch(session, fixtures, tags) {
    return postForm(
        session,
        MAP_SEARCH,
        { name: fixtures.pinNamePrefix },
        Object.assign({ endpoint: "map_search" }, tags),
        { timeout: fixtures.actorTimeout },
    );
}

/** Rename nothing and change one colour on the label every pin carries. The colour rather than the description on purpose. */
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
 * Queue a batch of pins through the confirmed import, then poll it to the end.
 *
 * The request returns once the selection is stored and queued, and a bulk worker writes the
 * pins. Polling to completion keeps the phase as long as the work it causes, so the neighbour is
 * measured while the import is actually running - the same thing the import dialog does.
 *
 * `auto_tag` is off so the run measures the import rather than the AI queue it would otherwise
 * fill.
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

    const accepted = postJson(
        session,
        IMPORT_CONFIRMED,
        {
            auto_tag: false,
            lists: [{ stem: "Perf Import", create_category: false, label_ids: [], pins }],
        },
        Object.assign({ endpoint: "import_confirmed" }, tags),
    );
    if (accepted.status !== 202) {
        return accepted;
    }

    const statusPath = accepted.json("status_url");
    const deadline = Date.now() + durationMs(fixtures.importTimeout);
    let polled = accepted;
    while (Date.now() < deadline) {
        sleep(1);
        polled = get(session, statusPath, Object.assign({ endpoint: "import_confirmed_status" }, tags));
        const state = polled.status === 200 ? polled.json("status") : "error";
        if (state === "done" || state === "error" || state === "cancelled") {
            break;
        }
    }
    return polled;
}

/** A k6 duration such as `"230s"`, `"4m"` or `"1h"`, in milliseconds. A bare number is seconds. */
function durationMs(value) {
    const match = /^(\d+(?:\.\d+)?)(ms|s|m|h)?$/.exec(String(value).trim());
    if (!match) {
        throw new Error(`Unreadable duration: ${value}`);
    }
    const scale = { ms: 1, s: 1000, m: 60000, h: 3600000 }[match[2] || "s"];
    return Number(match[1]) * scale;
}

/** Every action by the name `schedule.js` refers to it by. */
export const ACTIONS = { mapInit, mapSearch, labelEdit, importConfirmed };
