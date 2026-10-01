import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installGlobalPriorityList } from "./priority-list";

function item(slug: string, ranked: boolean): string {
    return `<li class="priority-list__item${ranked ? "" : " priority-list__item--unranked"}" data-slug="${slug}">
        <span class="priority-list__rank"></span>
        <button type="button" class="priority-list__btn" data-action="up" id="${slug}-up"></button>
        <button type="button" class="priority-list__btn" data-action="down" id="${slug}-down"></button>
        <input type="checkbox" data-role="rank-toggle" id="${slug}-toggle"${ranked ? " checked" : ""}>
    </li>`;
}

let changes = 0;

beforeAll(() => {
    installGlobalPriorityList();
});

beforeEach(() => {
    changes = 0;
    document.body.innerHTML = `<ul class="priority-list" data-hidden-id="order">${item("a", true)}${item("b", true)}${item("c", false)}</ul>
        <input type="hidden" id="order" value="a,b">`;
    document.getElementById("order")?.addEventListener("change", () => changes++);
});

const order = (): string => (document.getElementById("order") as HTMLInputElement | null)?.value ?? "";
const ranks = (): string[] => Array.from(document.querySelectorAll(".priority-list__rank")).map((r) => r.textContent ?? "");
const click = (id: string): void => document.getElementById(id)?.click();

describe("priority list", () => {
    test("moving an item up reorders the saved value and renumbers", () => {
        click("b-up");
        expect(order()).toBe("b,a");
        expect(ranks()).toEqual(["1", "2", "—"]);
        expect(changes).toBe(1);
    });

    test("an item cannot move down past the ranked ones", () => {
        click("b-down");
        expect(order()).toBe("a,b");
    });

    test("ranking an unranked item puts it last among the ranked", () => {
        click("c-toggle");
        expect(order()).toBe("a,b,c");
        expect(ranks()).toEqual(["1", "2", "3"]);
    });

    test("unranking an item drops it from the saved value", () => {
        click("a-toggle");
        expect(order()).toBe("b");
        expect(ranks()).toEqual(["1", "—", "—"]);
    });

    test("a list without its hidden input is left alone", () => {
        document.getElementById("order")?.remove();
        click("b-up");
        expect(Array.from(document.querySelectorAll("li")).map((li) => (li as HTMLElement).dataset.slug)).toEqual(["a", "b", "c"]);
    });
});
