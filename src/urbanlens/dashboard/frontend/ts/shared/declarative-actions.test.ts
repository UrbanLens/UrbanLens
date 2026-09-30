import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installDeclarativeActions } from "./declarative-actions";

const realConfirmDialog = window.confirmDialog;
let answer = true;
let asked: { title?: string; message?: string; confirmLabel?: string }[] = [];
let submitted: (string | null)[] = [];

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeAll(() => {
    installDeclarativeActions();
    installDeclarativeActions();
    window.addEventListener("submit", recordSubmit);
    window.confirmDialog = async (options) => {
        asked.push(typeof options === "string" ? { message: options } : options);
        return answer;
    };
});

afterAll(() => {
    window.removeEventListener("submit", recordSubmit);
    window.confirmDialog = realConfirmDialog;
});

beforeEach(() => {
    answer = true;
    asked = [];
    submitted = [];
});

// Stands in for the navigation, after every document listener has had its say: record what went, and stop there.
function recordSubmit(event: Event): void {
    if (event.defaultPrevented || !(event instanceof SubmitEvent)) return;
    submitted.push(event.submitter?.getAttribute("name") ?? null);
    event.preventDefault();
}

function render(markup: string): HTMLFormElement {
    document.body.innerHTML = markup;
    const form = document.querySelector("form");
    if (!form) throw new Error("no form");
    return form;
}

describe("data-confirm", () => {
    test("on a form, asks first and submits once confirmed", async () => {
        render(`<form data-confirm="Revoke this API key?" data-confirm-label="Revoke"><button type="submit" name="go">Go</button></form>`);
        document.querySelector("button")?.click();
        expect(submitted).toEqual([]);
        await settle();
        expect(asked).toEqual([{ title: "Revoke this API key?", message: undefined, confirmLabel: "Revoke" }]);
        expect(submitted).toEqual(["go"]);
    });

    test("declined, nothing is submitted", async () => {
        answer = false;
        render(`<form data-confirm="Remove this passkey?"><button type="submit">Go</button></form>`);
        document.querySelector("button")?.click();
        await settle();
        expect(submitted).toEqual([]);
    });

    test("on the submit button, only that button asks", async () => {
        render(`<form><button type="submit" name="save">Save</button><button type="submit" name="block" data-confirm="Block owl?">Block</button></form>`);
        document.querySelector<HTMLElement>('[name="save"]')?.click();
        await settle();
        expect(asked).toEqual([]);
        document.querySelector<HTMLElement>('[name="block"]')?.click();
        await settle();
        expect(asked.map((a) => a.title)).toEqual(["Block owl?"]);
        expect(submitted).toEqual(["save", "block"]);
    });

    test("asks again the next time", async () => {
        render(`<form data-confirm="Generate new backup codes?"><button type="submit">Go</button></form>`);
        document.querySelector("button")?.click();
        await settle();
        document.querySelector("button")?.click();
        await settle();
        expect(asked.length).toBe(2);
        expect(submitted.length).toBe(2);
    });
});

describe("data-reload", () => {
    test("reloads the page", () => {
        let reloads = 0;
        const realLocation = window.location;
        Object.defineProperty(window, "location", { value: { reload: () => void reloads++ }, configurable: true });
        try {
            document.body.innerHTML = `<button type="button" data-reload><i>refresh</i> Try again</button>`;
            document.querySelector<HTMLElement>("[data-reload] i")?.click();
        } finally {
            Object.defineProperty(window, "location", { value: realLocation, configurable: true });
        }
        expect(reloads).toBe(1);
    });
});
