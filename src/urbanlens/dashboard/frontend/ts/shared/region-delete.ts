/**
 * A delete tool for maps of drawn regions that commits each deletion when it is clicked.
 *
 * leaflet-draw's own remove tool only stages a deletion: it commits through that tool's Save action,
 * and starting any other draw or edit tool reverts it, so the deleted region comes back.
 */

declare const L: typeof import("leaflet");

/** Arms and disarms deleting a clicked layer of one group. */
export class ImmediateDeleteMode {
    private armed = false;
    private readonly group: L.FeatureGroup;
    private readonly onDelete: () => void;
    private readonly onArmedChange: (armed: boolean) => void;

    /**
     * @param group - The layers a click may delete.
     * @param onDelete - Called after each deletion, to persist the remaining layers.
     * @param onArmedChange - Called whenever the mode arms or disarms.
     */
    constructor(group: L.FeatureGroup, onDelete: () => void, onArmedChange: (armed: boolean) => void = () => {}) {
        this.group = group;
        this.onDelete = onDelete;
        this.onArmedChange = onArmedChange;
    }

    get isArmed(): boolean {
        return this.armed;
    }

    arm(): void {
        if (this.armed || this.group.getLayers().length === 0) return;
        this.armed = true;
        this.group.on("click", this.deleteClicked);
        this.onArmedChange(true);
    }

    disarm(): void {
        if (!this.armed) return;
        this.armed = false;
        this.group.off("click", this.deleteClicked);
        this.onArmedChange(false);
    }

    toggle(): void {
        if (this.armed) this.disarm();
        else this.arm();
    }

    private readonly deleteClicked = (e: L.LeafletEvent): void => {
        const layer: L.Layer | undefined = e.propagatedFrom ?? e.layer;
        if (!layer) return;
        this.group.removeLayer(layer);
        this.onDelete();
        if (this.group.getLayers().length === 0) this.disarm();
    };
}

/**
 * Add a delete button to a map whose draw control was built with `edit.remove: false`.
 *
 * @param map - The map to add the button to.
 * @param group - The drawn regions a click may delete.
 * @param onDelete - Called after each deletion, to persist the remaining regions.
 * @returns The mode the button toggles.
 */
export function addRegionDeleteControl(map: L.Map, group: L.FeatureGroup, onDelete: () => void): ImmediateDeleteMode {
    const container = L.DomUtil.create("div", "leaflet-draw-section");
    const bar = L.DomUtil.create("div", "leaflet-draw-toolbar leaflet-bar", container);
    const button = L.DomUtil.create("a", "leaflet-draw-edit-remove", bar);
    button.href = "#";
    button.title = "Delete a region: choose this, then click the region";
    button.setAttribute("role", "button");
    button.setAttribute("aria-pressed", "false");

    const mode = new ImmediateDeleteMode(group, onDelete, (armed) => {
        button.classList.toggle("leaflet-draw-toolbar-button-enabled", armed);
        button.setAttribute("aria-pressed", String(armed));
    });
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(button, "click", (e) => {
        L.DomEvent.preventDefault(e);
        mode.toggle();
    });

    const control = new L.Control({ position: "topleft" });
    control.onAdd = () => container;
    control.addTo(map);
    map.on("draw:drawstart draw:editstart", () => mode.disarm());
    return mode;
}

type RegionGeoJson = GeoJSON.Geometry | GeoJSON.Feature | GeoJSON.FeatureCollection;

/**
 * Split a stored region into one polygon per part.
 *
 * `L.geoJSON` makes one layer per geometry, so a stored MultiPolygon would load as a single layer that the
 * delete and edit tools can only act on as a whole.
 *
 * @param value - The stored region, as GeoJSON.
 * @returns Its polygons; anything that is not a polygon is dropped.
 */
export function polygonParts(value: RegionGeoJson | null | undefined): GeoJSON.Polygon[] {
    if (!value) return [];
    switch (value.type) {
        case "Polygon":
            return [value];
        case "MultiPolygon":
            return value.coordinates.map((coordinates) => ({ type: "Polygon", coordinates }));
        case "GeometryCollection":
            return value.geometries.flatMap((geometry) => polygonParts(geometry));
        case "Feature":
            return polygonParts(value.geometry);
        case "FeatureCollection":
            return value.features.flatMap((feature) => polygonParts(feature));
        default:
            return [];
    }
}

export function installGlobalRegionDelete(): void {
    window.RegionDelete = { add: addRegionDeleteControl, polygonParts };
}

declare global {
    interface Window {
        RegionDelete: { add: typeof addRegionDeleteControl; polygonParts: typeof polygonParts };
    }
}
