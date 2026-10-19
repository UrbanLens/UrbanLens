/** Identity of a Places layer marker, which a place with no Google id still has. */

interface KeyedPlace {
    place_id?: string;
    place_source?: string;
    source?: string;
    source_id?: string;
}

/** Its Google id, else the dataset record it came from; empty when it has neither. */
export function placesMarkerKey(place: KeyedPlace): string {
    if (place.place_id) return place.place_id;
    return place.source_id ? `${place.place_source || place.source}:${place.source_id}` : "";
}

/** Drop the marker under `key`, once the user's own pin stands for the place. */
export function forgetPlacesMarker<M>(markers: Map<string, M>, group: { removeLayer(marker: M): unknown }, key: string | null): void {
    if (!key) return;
    const marker = markers.get(key);
    if (!marker) return;
    group.removeLayer(marker);
    markers.delete(key);
}
