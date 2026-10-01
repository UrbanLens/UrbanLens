/**
 * Reads the filter panel's result set out of a swapped `#map-body`
 * (templates/dashboard/pages/map/data.html and data_ids.html). The partials carry
 * data only; the map page applies it.
 */

export interface MapFilterMeta {
    truncated: boolean;
    total: number;
}

/** A result set shipped as full pin payloads, with the label dictionary those pins name. */
export interface MapFilterPayloads<Pin, Labels> {
    kind: "payloads";
    pins: Pin[];
    labels: Labels | null;
    meta: MapFilterMeta;
}

/** A result set shipped as identifiers, for a client whose store already holds every pin. */
export interface MapFilterIdentifiers {
    kind: "identifiers";
    uuids: string[];
    meta: MapFilterMeta;
}

export type MapFilterResults<Pin, Labels> = MapFilterPayloads<Pin, Labels> | MapFilterIdentifiers;

function jsonText(root: ParentNode, id: string): string | null {
    const el = root.querySelector(`script#${id}[type="application/json"]`);
    return el && el.textContent ? el.textContent : null;
}

function readJson(root: ParentNode, id: string): unknown {
    const text = jsonText(root, id);
    return text === null ? undefined : JSON.parse(text);
}

function readMeta(root: ParentNode): MapFilterMeta {
    const raw = readJson(root, "map-filter-meta");
    if (!raw || typeof raw !== "object") return { truncated: false, total: 0 };
    const truncated = "truncated" in raw && raw.truncated === true;
    const total = "total" in raw && typeof raw.total === "number" ? raw.total : 0;
    return { truncated, total };
}

/**
 * Parses the result documents under *root*.
 *
 * Args:
 *     root: The swapped `#map-body`, or any node containing its documents.
 *
 * Returns:
 *     The result set, or null when *root* carries neither a payload nor an identifier document.
 */
export function readMapFilterResults<Pin, Labels>(root: ParentNode): MapFilterResults<Pin, Labels> | null {
    const uuids = readJson(root, "map-filter-uuids");
    if (Array.isArray(uuids)) {
        return { kind: "identifiers", uuids: uuids.filter((u): u is string => typeof u === "string"), meta: readMeta(root) };
    }
    const pins = readJson(root, "map-filter-pins");
    if (Array.isArray(pins)) {
        const labelsText = jsonText(root, "map-filter-labels");
        const labels: Labels | null = labelsText === null ? null : JSON.parse(labelsText);
        return { kind: "payloads", pins, labels, meta: readMeta(root) };
    }
    return null;
}
