/**
 * P128: a label's `icon` reached the add-pin dialog's chips and suggestions as raw HTML.
 */
import { describe, expect, test } from "bun:test";
import { labelChip, labelSuggestion, type LabelCandidate } from "./add-pin-label-chips";

const HOSTILE: LabelCandidate = {
    id: 7,
    name: '<b id="name-probe">n</b>',
    icon: '<img src=x onerror="window.__p128=1">',
    kind: 'tag"><img src=x onerror="window.__p128k=1">',
};

function assertInert(el: HTMLElement): void {
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector("#name-probe")).toBeNull();
    expect(el.querySelector("[onerror]")).toBeNull();
}

describe("add-pin label chips render label text as text", () => {
    test("a chip shows a hostile icon and name literally", () => {
        const chip = labelChip(HOSTILE, () => {});
        assertInert(chip);
        expect(chip.querySelector(".apdlg-chip-icon")?.textContent).toBe(HOSTILE.icon);
        expect(chip.querySelector(".apdlg-chip-name")?.textContent).toBe(HOSTILE.name);
    });

    test("a suggestion shows a hostile icon, name and kind literally", () => {
        const item = labelSuggestion(HOSTILE, () => {});
        assertInert(item);
        expect(item.querySelector(".apdlg-sugg-icon")?.textContent).toBe(HOSTILE.icon);
        expect(item.querySelector(".apdlg-sugg-name")?.textContent).toBe(HOSTILE.name);
        expect(item.querySelector(".apdlg-sugg-kind")?.textContent).toBe(HOSTILE.kind);
    });

    test("no icon means no icon element", () => {
        const plain = { id: 1, name: "Hospital", kind: "category" };
        expect(labelChip(plain, () => {}).querySelector(".apdlg-chip-icon")).toBeNull();
        expect(labelSuggestion(plain, () => {}).querySelector(".apdlg-sugg-icon")).toBeNull();
    });

    test("an emoji or material name still shows", () => {
        const item = labelSuggestion({ id: 2, name: "Asylum", icon: "🏥", kind: "category" }, () => {});
        expect(item.querySelector(".apdlg-sugg-icon")?.textContent).toBe("🏥");
        expect(item.querySelector(".apdlg-sugg-kind")?.className).toBe("apdlg-sugg-kind apdlg-sugg-kind--category");
    });

    test("the remove and select callbacks fire", () => {
        let removed = 0;
        let selected = 0;
        const chip = labelChip({ id: 3, name: "x" }, () => removed++);
        (chip.querySelector(".apdlg-chip-remove") as HTMLButtonElement).click();
        const item = labelSuggestion({ id: 3, name: "x" }, () => selected++);
        item.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        expect([removed, selected]).toEqual([1, 1]);
    });
});
