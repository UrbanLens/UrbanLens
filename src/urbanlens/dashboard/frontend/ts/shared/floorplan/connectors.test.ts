import { describe, expect, test } from "bun:test";

import type { Floor, Marker } from "./document";
import { CONNECTOR_KINDS, connectorCandidates } from "./connectors";

function floorAt(level: number, markers: Array<Partial<Marker>>): Floor {
    return {
        level,
        name: `Level ${level}`,
        walls: [],
        rooms: [],
        markers: markers.map((marker, index) => ({ kind: "stair", x: 0, y: 0, name: `m${level}-${index}`, ...marker }) as Marker),
    } as Floor;
}

describe("connectorCandidates", () => {
    test("a lift that skips storeys can still be linked", () => {
        // Only the floor above and below used to be offered, so an express lift
        // between the ground and third storeys - or a stair on a plan whose
        // middle floor has not been drawn yet - could not be linked at all.
        const ground = floorAt(0, [{ kind: "elevator", name: "express" }]);
        const third = floorAt(3, [{ kind: "elevator", name: "express-3" }]);
        const marker = ground.markers[0] as Marker;

        const found = connectorCandidates([ground, third], ground, marker);

        expect(found.map((entry) => entry.marker.name)).toEqual(["express-3"]);
    });

    test("the nearest storey is offered first", () => {
        const ground = floorAt(0, [{ name: "here" }]);
        const first = floorAt(1, [{ name: "one-up" }]);
        const fourth = floorAt(4, [{ name: "four-up" }]);
        const marker = ground.markers[0] as Marker;

        const found = connectorCandidates([fourth, first, ground], ground, marker);

        expect(found.map((entry) => entry.marker.name)).toEqual(["one-up", "four-up"]);
    });

    test("within a storey, the shaft overhead beats one across the building", () => {
        const ground = floorAt(0, [{ name: "here", x: 10, y: 10 }]);
        const first = floorAt(1, [
            { name: "far", x: 40, y: 40 },
            { name: "overhead", x: 10.2, y: 9.8 },
        ]);
        const marker = ground.markers[0] as Marker;

        const found = connectorCandidates([ground, first], ground, marker);

        expect(found.map((entry) => entry.marker.name)).toEqual(["overhead", "far"]);
    });

    test("a basement counts as one storey away, same as the floor above", () => {
        const ground = floorAt(0, [{ name: "here" }]);
        const basement = floorAt(-1, [{ name: "down" }]);
        const second = floorAt(2, [{ name: "up-two" }]);
        const marker = ground.markers[0] as Marker;

        const found = connectorCandidates([ground, second, basement], ground, marker);

        expect(found.map((entry) => entry.marker.name)).toEqual(["down", "up-two"]);
    });

    test("only connector kinds are offered", () => {
        const ground = floorAt(0, [{ name: "here" }]);
        const first = floorAt(1, [{ kind: "hazard", name: "hole" }, { kind: "elevator", name: "lift" }]);
        const marker = ground.markers[0] as Marker;

        const found = connectorCandidates([ground, first], ground, marker);

        expect(found.map((entry) => entry.marker.name)).toEqual(["lift"]);
        expect(CONNECTOR_KINDS.has("hazard" as Marker["kind"])).toBe(false);
    });

    test("the marker's own floor is never a candidate", () => {
        const ground = floorAt(0, [{ name: "here" }, { name: "other stair on this floor" }]);
        const marker = ground.markers[0] as Marker;

        expect(connectorCandidates([ground], ground, marker)).toEqual([]);
    });
});
