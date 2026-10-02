import { describe, expect, test } from "bun:test";
import { type Node as ProseMirrorNode, Schema } from "@tiptap/pm/model";
import { alignBlocks, joinRegion } from "./article-source";

const schema = new Schema({ nodes: { doc: { content: "block+" }, paragraph: { group: "block", content: "text*" }, text: {} } });
const p = (text: string): ProseMirrorNode => schema.nodes.paragraph!.create(null, text ? schema.text(text) : null);

describe("alignBlocks", () => {
    const [a, b, c, d] = [p("a"), p("b"), p("c"), p("d")];

    test("an unchanged document matches every block where it was", () => {
        expect(alignBlocks([[a], [b], [c]], [a, b, c])).toEqual([0, 1, 2]);
    });

    test("an edited block no longer matches; equal copies of the rest still do", () => {
        expect(alignBlocks([[a], [b], [c]], [p("a"), p("b!"), p("c")])).toEqual([0, -1, 2]);
    });

    test("deleted and inserted blocks shift the rest", () => {
        expect(alignBlocks([[a], [b], [c]], [a, c])).toEqual([0, -1, 1]);
        expect(alignBlocks([[a], [b], [c]], [a, d, b, c])).toEqual([0, 2, 3]);
    });

    test("edits at both ends still match the unchanged middle", () => {
        expect(alignBlocks([[a], [b], [c], [d]], [p("x"), b, c, p("y")])).toEqual([-1, 1, 2, -1]);
    });

    test("a block of several nodes matches only while all of them are still together", () => {
        expect(alignBlocks([[a, b], [c]], [a, b, c])).toEqual([0, 2]);
        expect(alignBlocks([[a, b], [c]], [a, d, b, c])).toEqual([-1, 3]);
    });

    test("keeps the most blocks in order when blocks move", () => {
        expect(alignBlocks([[a], [b], [c]], [c, a, b])).toEqual([1, 2, -1]);
    });
});

describe("joinRegion", () => {
    const middle = { atStart: false, atEnd: false };

    test("unchanged neighbours keep the gap between them", () => {
        expect(joinRegion(["\n\n\n"], "", middle)).toBe("\n\n\n");
    });

    test("a deleted block takes one gap with it and keeps the definitions from either", () => {
        expect(joinRegion(["\n\n", "\n\n"], "", middle)).toBe("\n\n");
        expect(joinRegion(["\n\n[r]: https://r.example\n\n", "\n\n"], "", middle)).toBe("\n\n[r]: https://r.example\n\n");
        expect(joinRegion(["\n\n", "\n\n[r]: https://r.example\n"], "", { atStart: false, atEnd: true })).toBe("\n\n[r]: https://r.example\n");
    });

    test("an edited block keeps the gaps around it", () => {
        expect(joinRegion(["\n\n\n", "\n\n"], "X", middle)).toBe("\n\n\nX\n\n");
    });

    test("new text gets a blank line either side, which a heading's single newline wasn't", () => {
        expect(joinRegion(["\n", "\n"], "X", middle)).toBe("\n\nX\n\n");
        expect(joinRegion(["\n[r]: https://r.example\n"], "X", middle)).toBe("\n[r]: https://r.example\n\nX\n\n");
    });

    test("a block added at either end keeps the document's leading and trailing text", () => {
        expect(joinRegion(["\n"], "X", { atStart: false, atEnd: true })).toBe("\n\nX\n");
        expect(joinRegion([""], "X", { atStart: true, atEnd: false })).toBe("X\n\n");
    });

    test("writing into an empty document keeps its definitions and final newline", () => {
        expect(joinRegion([""], "X", { atStart: true, atEnd: true })).toBe("X");
        expect(joinRegion(["[r]: https://r.example\n"], "X", { atStart: true, atEnd: true })).toBe("X\n\n[r]: https://r.example\n");
    });

    test("definitions between replaced blocks come after the new text", () => {
        expect(joinRegion(["\n\n", "\n\n[r]: https://r.example\n\n", "\n\n"], "X", middle)).toBe("\n\nX\n\n[r]: https://r.example\n\n");
    });
});
