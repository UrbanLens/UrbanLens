import { beforeAll, beforeEach, expect, test } from "bun:test";

import { installVisitForm } from "./visit-form";

beforeAll(() => {
    installVisitForm();
    installVisitForm();
});

const PANEL = `
  <form>
    <label class="visit-photo-upload"><span class="visit-photo-upload-text">Upload new photos</span><input type="file" name="photos" multiple></label>
    <div class="visit-attachment-panel">
      <div class="visit-external-existing" id="ext-3"><span>Ada</span><input type="hidden" name="external_remove" value="3" disabled><button type="button" data-external-remove>x</button></div>
      <div class="visit-external-list"></div>
      <button type="button" data-external-add>Add someone</button>
      <template class="visit-external-row-template">
        <div class="visit-external-row">
          <input type="text" name="external_name_{n}"><input type="email" name="external_email_{n}">
          <input type="checkbox" name="external_invite_{n}" checked>
          <button type="button" data-external-row-remove>x</button>
        </div>
      </template>
    </div>
  </form>`;

beforeEach(() => {
    document.body.innerHTML = PANEL;
});

const click = (sel: string) => document.querySelector(sel)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

test("choosing files says how many", () => {
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("input");
    Object.defineProperty(input, "files", { value: { length: 3 }, configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.querySelector(".visit-photo-upload-text")?.textContent).toBe("3 photo(s) selected");
    Object.defineProperty(input, "files", { value: { length: 0 }, configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.querySelector(".visit-photo-upload-text")?.textContent).toBe("Upload new photos");
});

test("removing someone already on the visit sends their removal and hides them", () => {
    click("[data-external-remove]");
    expect(document.querySelector<HTMLInputElement>('input[name="external_remove"]')?.disabled).toBe(false);
    expect(document.getElementById("ext-3")?.hidden).toBe(true);
});

test("each added person gets their own numbered fields, can be taken out again, and is focused", () => {
    click("[data-external-add]");
    click("[data-external-add]");
    const rows = document.querySelectorAll(".visit-external-list .visit-external-row");
    const names = Array.from(rows).map((row) => row.querySelector("input")?.name);
    expect(names).toHaveLength(2);
    expect(new Set(names).size).toBe(2);
    expect(names.every((name) => /^external_name_\d+$/.test(name ?? ""))).toBe(true);
    expect(document.activeElement).toBe(rows[1]?.querySelector("input") ?? null);
    rows[0]?.querySelector<HTMLElement>("[data-external-row-remove]")?.click();
    expect(document.querySelectorAll(".visit-external-list .visit-external-row")).toHaveLength(1);
});
