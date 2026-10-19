import { describe, expect, test } from "bun:test";

import { toPostedResults } from "./google-pse";

describe("toPostedResults", () => {
    test("keeps http(s) results in the endpoint's shape", () => {
        expect(toPostedResults([{ unescapedUrl: "https://a.example/x", titleNoFormatting: "T", contentNoFormatting: "S" }])).toEqual([
            { url: "https://a.example/x", title: "T", snippet: "S" },
        ]);
    });

    test("drops non-http schemes and missing urls", () => {
        expect(toPostedResults([{ url: "javascript:alert(1)" }, { url: "ftp://a/b" }, {}, null])).toEqual([]);
    });

    test("caps the count and tolerates non-arrays", () => {
        const many = Array.from({ length: 50 }, (_, i) => ({ url: `https://a.example/${i}` }));
        expect(toPostedResults(many)).toHaveLength(20);
        expect(toPostedResults("nope")).toEqual([]);
    });
});
