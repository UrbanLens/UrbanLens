import { describe, expect, test } from "bun:test";

import { type FloorLike, contiguousLevels, deriveDesignations, leadingInt } from "./designations";

/** Floors at the given levels, all with blank designations. */
const stack = (...levels: number[]): FloorLike[] => levels.map((level) => ({ level }));

/** The derived labels, in stack order from the bottom up. */
function labelsOf(floors: readonly FloorLike[]): string[] {
    const map = deriveDesignations(floors);
    return [...floors].sort((a, b) => a.level - b.level).map((floor) => map.get(floor) as string);
}

describe("leadingInt", () => {
    test("reads the number a designation starts with", () => {
        expect(leadingInt("4")).toBe(4);
        expect(leadingInt("14")).toBe(14);
        expect(leadingInt("4A")).toBe(4);
        expect(leadingInt(" 7B ")).toBe(7);
    });

    test("is null when the designation does not start with digits", () => {
        for (const value of ["M", "PH", "LG", "G", "", "   ", "A4"]) {
            expect(leadingInt(value)).toBeNull();
        }
    });
});

describe("deriveDesignations", () => {
    test("an ordinary building is G then 1, 2, 3", () => {
        expect(labelsOf(stack(0, 1, 2, 3))).toEqual(["G", "1", "2", "3"]);
    });

    test("basements count downward from the datum", () => {
        expect(labelsOf(stack(-2, -1, 0, 1))).toEqual(["B2", "B1", "G", "1"]);
    });

    test("the ground datum does not consume the first storey number", () => {
        // A building's "1" sits above its ground floor, not on it.
        const labels = labelsOf(stack(0, 1));
        expect(labels).toEqual(["G", "1"]);
    });

    test("skipping 13 renumbers by designation while levels stay contiguous", () => {
        const floors: FloorLike[] = [{ level: 0 }, { level: 11 }, { level: 12 }, { level: 13, designation: "14" }, { level: 14 }];
        for (let level = 1; level <= 10; level++) floors.push({ level });

        const map = deriveDesignations(floors);
        const byLevel = (level: number): string => map.get(floors.find((f) => f.level === level) as FloorLike) as string;

        expect(byLevel(12)).toBe("12");
        expect(byLevel(13)).toBe("14");
        // The count resumes past the typed number, so nothing is called 14 twice.
        expect(byLevel(14)).toBe("15");
    });

    test("4A and 4B do not consume the number above them", () => {
        const floors: FloorLike[] = [
            { level: 0 },
            { level: 1 },
            { level: 2 },
            { level: 3 },
            { level: 4, designation: "4A" },
            { level: 5, designation: "4B" },
            { level: 6 },
        ];

        expect(labelsOf(floors)).toEqual(["G", "1", "2", "3", "4A", "4B", "5"]);
    });

    test("a mezzanine sits between storeys without taking a number", () => {
        const floors: FloorLike[] = [{ level: 0 }, { level: 1, designation: "M" }, { level: 2 }, { level: 3 }];

        expect(labelsOf(floors)).toEqual(["G", "M", "1", "2"]);
    });

    test("a typed basement depth re-anchors the ones below it", () => {
        const floors: FloorLike[] = [{ level: 0 }, { level: -1 }, { level: -2, designation: "B4" }, { level: -3 }];

        expect(labelsOf(floors)).toEqual(["B5", "B4", "B1", "G"]);
    });

    test("the nickname never affects the numbering", () => {
        const plain = stack(0, 1, 2);
        const named = [
            { level: 0, name: "Shop" },
            { level: 1, name: "Level 7" },
            { level: 2, name: "Boiler" },
        ] as Array<FloorLike & { name: string }>;

        expect(labelsOf(named)).toEqual(labelsOf(plain));
    });

    test("input order does not matter", () => {
        expect(labelsOf(stack(2, 0, -1, 1))).toEqual(labelsOf(stack(-1, 0, 1, 2)));
    });

    test("every floor gets a label", () => {
        const floors = stack(-2, -1, 0, 1, 2, 3);
        const map = deriveDesignations(floors);

        expect(map.size).toBe(floors.length);
        for (const floor of floors) expect((map.get(floor) as string).length).toBeGreaterThan(0);
    });

    test("blank and whitespace designations derive rather than showing empty", () => {
        const floors: FloorLike[] = [{ level: 0, designation: "" }, { level: 1, designation: "   " }];

        expect(labelsOf(floors)).toEqual(["G", "1"]);
    });

    test("no floors derives nothing", () => {
        expect(deriveDesignations([]).size).toBe(0);
    });

    test("a plan with no ground datum still counts from one", () => {
        expect(labelsOf(stack(1, 2, 3))).toEqual(["1", "2", "3"]);
    });
});

describe("contiguousLevels", () => {
    test("leaves an already-contiguous stack alone", () => {
        const floors = stack(-1, 0, 1, 2);
        expect(contiguousLevels(floors).map((r) => r.level)).toEqual([-1, 0, 1, 2]);
    });

    test("closes the gap left by deleting a middle floor", () => {
        // "Floor 1, Floor 2, Floor 4" is exactly what this prevents.
        expect(contiguousLevels(stack(0, 1, 3)).map((r) => r.level)).toEqual([0, 1, 2]);
    });

    test("separates floors that collided on one level", () => {
        const floors = stack(0, 1, 1, 2);
        expect(contiguousLevels(floors).map((r) => r.level)).toEqual([0, 1, 2, 3]);
    });

    test("keeps the storey nearest the old datum as the new datum", () => {
        const result = contiguousLevels(stack(-5, 3, 9));
        expect(result.map((r) => r.level)).toEqual([-1, 0, 1]);
    });

    test("returns floors in stack order", () => {
        const result = contiguousLevels(stack(2, -1, 0));
        expect(result.map((r) => r.floor.level)).toEqual([-1, 0, 2]);
    });

    test("does not modify the input", () => {
        const floors = stack(0, 3, 7);
        const before = structuredClone(floors);
        contiguousLevels(floors);
        expect(floors).toEqual(before);
    });

    test("no floors renumbers to nothing", () => {
        expect(contiguousLevels([])).toEqual([]);
    });

    test("renumbering then deriving gives a gapless set of labels", () => {
        const floors = stack(0, 1, 3, 8);
        const repaired = contiguousLevels(floors).map(({ floor, level }) => ({ ...floor, level }));

        expect(labelsOf(repaired)).toEqual(["G", "1", "2", "3"]);
    });
});
