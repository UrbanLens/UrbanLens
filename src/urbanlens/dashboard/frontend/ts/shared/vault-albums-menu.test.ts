/**
 * Vault > Photos' Albums overflow menu and the lazily loaded cross-pin albums panel.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { initVaultAlbumsMenu } from "./vault-albums-menu";

let ajax: Array<[string, string]>;

beforeEach(() => {
    ajax = [];
    window.htmx = { process: () => undefined, trigger: () => undefined, ajax: async (verb, url) => void ajax.push([verb, url]) };
    document.body.innerHTML =
        '<div class="pin-list-more-menu">' +
        '<button id="vault-albums-more-btn" aria-expanded="false"></button>' +
        '<div id="vault-albums-more-menu-panel" hidden><button id="vault-pin-albums-toggle-btn"></button></div>' +
        "</div>" +
        '<p id="elsewhere"></p>' +
        '<div id="vault-pin-albums-panel" data-url="/vault/photos/pin-albums/" hidden></div>';
    Element.prototype.scrollIntoView = () => undefined;
    initVaultAlbumsMenu();
});

afterEach(() => {
    document.body.innerHTML = "";
});

function el(id: string): HTMLElement {
    return document.getElementById(id)!;
}

describe("the albums menu", () => {
    test("the button toggles the panel and says whether it is open", () => {
        el("vault-albums-more-btn").click();
        expect(el("vault-albums-more-menu-panel").hidden).toBe(false);
        expect(el("vault-albums-more-btn").getAttribute("aria-expanded")).toBe("true");
        el("vault-albums-more-btn").click();
        expect(el("vault-albums-more-menu-panel").hidden).toBe(true);
    });

    test("a click elsewhere or Escape closes it", () => {
        el("vault-albums-more-btn").click();
        el("elsewhere").click();
        expect(el("vault-albums-more-menu-panel").hidden).toBe(true);

        el("vault-albums-more-btn").click();
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        expect(el("vault-albums-more-menu-panel").hidden).toBe(true);
        expect(el("vault-albums-more-btn").getAttribute("aria-expanded")).toBe("false");
    });

    test("showing pin albums reveals the panel and loads it once", () => {
        el("vault-albums-more-btn").click();
        el("vault-pin-albums-toggle-btn").click();
        el("vault-pin-albums-toggle-btn").click();
        expect(el("vault-pin-albums-panel").hidden).toBe(false);
        expect(el("vault-albums-more-menu-panel").hidden).toBe(true);
        expect(ajax).toEqual([["GET", "/vault/photos/pin-albums/"]]);
    });
});
