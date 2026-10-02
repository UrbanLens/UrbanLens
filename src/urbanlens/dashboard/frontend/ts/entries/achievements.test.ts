import { beforeEach, describe, expect, test } from "bun:test";

import { installPickerActions } from "../shared/picker-actions";
import "./achievements";

installPickerActions();

beforeEach(() => {
    document.body.innerHTML = `
      <div class="admin-achievement-medal" id="achv-medal-achv-3" style="--achievement-color: #111111"></div>
      <div class="color-picker" id="achv-color-achv-3" data-color-value-id="achv-color-value-achv-3" data-color-medal-id="achv-medal-achv-3">
        <input type="hidden" name="color" id="achv-color-value-achv-3" value="#111111">
        <button type="button" class="color-swatch selected" id="dark" data-color="#111111"></button>
        <button type="button" class="color-swatch" id="gold" data-color="#d4a017"></button>
      </div>`;
});

describe("an achievement's colour swatch", () => {
    test("fills the colour field, marks the swatch and tints the medal above it", () => {
        document.getElementById("gold")!.click();

        expect((document.getElementById("achv-color-value-achv-3") as HTMLInputElement).value).toBe("#d4a017");
        expect(document.getElementById("gold")!.classList.contains("selected")).toBe(true);
        expect(document.getElementById("dark")!.classList.contains("selected")).toBe(false);
        expect(document.getElementById("achv-medal-achv-3")!.style.getPropertyValue("--achievement-color")).toBe("#d4a017");
    });
});
