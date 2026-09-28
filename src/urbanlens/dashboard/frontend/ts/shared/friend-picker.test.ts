import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import type { HtmxApi } from "../types/globals";
import { clearFriendSelection, FRIEND_PICKER_RELOAD, installFriendPicker, mountFriendPicker, pickFriendsToInvite, selectedFriendIds } from "./friend-picker";

/** What GameFriendPickerView renders (partials/games/_friend_picker_options.html). */
function options(...friends: Array<[number, string]>): string {
    return friends
        .map(
            ([id, name]) =>
                `<label class="ul-game-friend-option"><span class="ul-checkbox-wrap"><input type="checkbox" name="invite_profile_ids" value="${id}"><span class="ul-checkbox"></span></span><span>${name}</span></label>`,
        )
        .join("");
}

const EMPTY = '<p class="ul-game-friend-picker__hint" data-friend-picker-empty>No friends available to invite.</p>';

function tick(root: ParentNode, id: number): void {
    root.querySelector<HTMLInputElement>(`input[value="${id}"]`)!.checked = true;
}

interface HtmxCalls {
    processed: Element[];
    triggered: Array<[Element, string]>;
}

function stubHtmx(): HtmxCalls {
    const calls: HtmxCalls = { processed: [], triggered: [] };
    const htmx: HtmxApi = {
        process: (element) => calls.processed.push(element),
        trigger: (element, event) => calls.triggered.push([element, event]),
        ajax: () => {},
    };
    window.htmx = htmx;
    return calls;
}

/** Stand-in for htmx answering the picker's request. */
function answer(list: HTMLElement, html: string): void {
    list.innerHTML = html;
    list.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
}

function openDialog(): HTMLDialogElement {
    return document.querySelector<HTMLDialogElement>("dialog.ul-game-invite-dialog")!;
}

function button(dialog: HTMLDialogElement, label: string): HTMLButtonElement {
    return Array.from(dialog.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === label)!;
}

beforeEach(() => {
    document.body.innerHTML = "";
});

afterEach(() => {
    delete window.htmx;
});

describe("selection", () => {
    test("the ticked boxes are the selection", () => {
        document.body.innerHTML = `<div id="list">${options([3, "ana"], [5, "bo"], [8, "cy"])}</div>`;
        const list = document.getElementById("list")!;
        tick(list, 3);
        tick(list, 8);
        expect(selectedFriendIds(list)).toEqual([3, 8]);
    });

    test("clearing unticks what the user sees, so a reset cannot leave boxes ticked that the next start ignores", () => {
        document.body.innerHTML = `<div id="list">${options([3, "ana"], [5, "bo"])}</div>`;
        const list = document.getElementById("list")!;
        tick(list, 3);
        clearFriendSelection(list);
        expect(selectedFriendIds(list)).toEqual([]);
        expect(list.querySelectorAll("input:checked")).toHaveLength(0);
    });

    test("nothing but profile ids is read back", () => {
        document.body.innerHTML = '<div id="list"><input type="checkbox" name="invite_profile_ids" value="x" checked><input type="checkbox" name="other" value="4" checked></div>';
        expect(selectedFriendIds(document.getElementById("list")!)).toEqual([]);
    });
});

describe("mountFriendPicker", () => {
    test("asks htmx for the list, leaving out who is already in the game", () => {
        const calls = stubHtmx();
        const list = document.createElement("div");
        document.body.appendChild(list);
        mountFriendPicker(list, "/games/friends/", [4, 9, 4]);
        expect(list.getAttribute("hx-get")).toBe("/games/friends/?exclude=4,9");
        expect(list.getAttribute("hx-trigger")).toBe(`load, ${FRIEND_PICKER_RELOAD}`);
        expect(list.hasAttribute("data-friend-picker")).toBe(true);
        expect(list.textContent).toContain("Loading friends");
        expect(calls.processed).toEqual([list]);
    });

    test("an empty exclusion leaves the URL alone", () => {
        const list = document.createElement("div");
        mountFriendPicker(list, "/games/friends/");
        expect(list.getAttribute("hx-get")).toBe("/games/friends/");
    });
});

describe("a failed load", () => {
    test("offers a retry that reloads the picker", () => {
        const calls = stubHtmx();
        installFriendPicker();
        document.body.innerHTML = '<div id="list" data-friend-picker><p>Loading friends…</p></div>';
        const list = document.getElementById("list")!;

        list.dispatchEvent(new CustomEvent("htmx:responseError", { bubbles: true }));
        expect(list.textContent).toContain("Couldn't load your friends list");

        list.querySelector<HTMLButtonElement>("[data-friend-picker-retry]")!.click();
        expect(list.textContent).toContain("Loading friends");
        expect(calls.triggered).toEqual([[list, FRIEND_PICKER_RELOAD]]);
    });

    test("leaves other htmx elements alone", () => {
        installFriendPicker();
        document.body.innerHTML = '<div id="other">content</div>';
        const other = document.getElementById("other")!;
        other.dispatchEvent(new CustomEvent("htmx:responseError", { bubbles: true }));
        expect(other.textContent).toBe("content");
    });
});

describe("pickFriendsToInvite", () => {
    test("mounts where the game asks and loads without the excluded profiles", async () => {
        stubHtmx();
        const shell = document.createElement("div");
        document.body.appendChild(shell);
        const picked = pickFriendsToInvite({ url: "/games/friends/", exclude: [2], mount: (dialog) => shell.appendChild(dialog) });
        const dialog = openDialog();
        expect(dialog.parentElement).toBe(shell);
        expect(dialog.querySelector("[data-friend-picker]")!.getAttribute("hx-get")).toBe("/games/friends/?exclude=2");
        button(dialog, "Cancel").click();
        expect(await picked).toEqual([]);
        expect(dialog.isConnected).toBe(false);
    });

    test("Invite resolves with the ticked friends", async () => {
        const picked = pickFriendsToInvite({ url: "/games/friends/", exclude: [], mount: (dialog) => document.body.appendChild(dialog) });
        const dialog = openDialog();
        const list = dialog.querySelector<HTMLElement>("[data-friend-picker]")!;
        answer(list, options([3, "ana"], [5, "bo"]));
        tick(list, 5);
        button(dialog, "Invite").click();
        expect(await picked).toEqual([5]);
    });

    test("Invite stays disabled until there is someone to invite", () => {
        void pickFriendsToInvite({ url: "/games/friends/", exclude: [], mount: (dialog) => document.body.appendChild(dialog) });
        const dialog = openDialog();
        const list = dialog.querySelector<HTMLElement>("[data-friend-picker]")!;
        expect(button(dialog, "Invite").disabled).toBe(true);
        answer(list, EMPTY);
        expect(button(dialog, "Invite").disabled).toBe(true);
        answer(list, options([3, "ana"]));
        expect(button(dialog, "Invite").disabled).toBe(false);
    });

    test("Escape counts as cancelling", async () => {
        const picked = pickFriendsToInvite({ url: "/games/friends/", exclude: [], mount: (dialog) => document.body.appendChild(dialog) });
        const dialog = openDialog();
        answer(dialog.querySelector<HTMLElement>("[data-friend-picker]")!, options([3, "ana"]));
        tick(dialog, 3);
        dialog.dispatchEvent(new Event("cancel"));
        expect(await picked).toEqual([]);
    });
});
