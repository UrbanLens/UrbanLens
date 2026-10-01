import type { ShapeSpec } from "./markup-engine";
import type { MarkupItem } from "./markup-toolbar";

const METERS_PER_DEGREE_LAT = 111_320;

/**
 * Converts a stored MarkupItem (DB GeoJSON-ish geometry) into the snapshot ShapeSpec format (`latlngs` as [lat,lng] pairs).
 */
export function markupItemToShapeSpec(item: MarkupItem): ShapeSpec | null {
    const geometry = item.geometry;
    const coordinates = geometry?.coordinates;
    if (coordinates == null) return null;

    const shape: ShapeSpec = {
        type: item.markup_type === "square" ? "rect" : (item.markup_type as ShapeSpec["type"]),
        latlngs: [],
        color: item.color,
        stroke_width: item.stroke_width,
        fill_opacity: item.fill_opacity,
        border_opacity: item.border_opacity,
    };
    if (item.border_color) shape.border_color = item.border_color;
    if (item.label) shape.label = item.label;

    try {
        if (item.markup_type === "line" || item.markup_type === "arrow") {
            shape.latlngs = (coordinates as [number, number][]).map((c) => [c[1], c[0]]);
        } else if (item.markup_type === "text") {
            const latlngs: [number, number][] = [[coordinates[1], coordinates[0]]];
            const boxCorner = geometry.box_corner;
            if (boxCorner) latlngs.push([boxCorner[1], boxCorner[0]]);
            shape.latlngs = latlngs;
            shape.label = item.label || "";
        } else if (item.markup_type === "circle") {
            const [lng, lat] = coordinates as [number, number];
            const radius = geometry.radius || 0;
            const dlng = radius / (METERS_PER_DEGREE_LAT * Math.max(Math.cos((lat * Math.PI) / 180), 1e-6));
            shape.latlngs = [
                [lat, lng],
                [lat, lng + dlng],
            ];
        } else if (item.markup_type === "square") {
            const ring = (coordinates as [number, number][][])[0]!;
            shape.latlngs = [
                [ring[0]![1], ring[0]![0]],
                [ring[2]![1], ring[2]![0]],
            ];
        } else if (item.markup_type === "polygon") {
            const ring = (coordinates as [number, number][][])[0]!;
            const first = ring[0]!;
            const last = ring[ring.length - 1]!;
            const points = ring.length > 1 && first[0] === last[0] && first[1] === last[1] ? ring.slice(0, -1) : ring;
            shape.latlngs = points.map((c) => [c[1], c[0]]);
        } else {
            return null;
        }
    } catch {
        return null;
    }
    if (!shape.latlngs.length) return null;
    return shape;
}
