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

describe("data-enabled-by", () => {
    test("a button stays disabled while the checkbox it names is unchecked", () => {
        render(`<form><input type="checkbox" id="tos"><input type="checkbox" id="other" checked><button type="submit" data-enabled-by="tos" disabled>Go</button></form>`);
        const box = document.getElementById("tos");
        const other = document.getElementById("other");
        const button = document.querySelector("button");
        if (!(box instanceof HTMLInputElement) || !(other instanceof HTMLInputElement) || !button) throw new Error("markup");
        box.click();
        expect(button.disabled).toBe(false);
        other.click();
        expect(button.disabled).toBe(false);
        box.click();
        expect(button.disabled).toBe(true);
    });

    test("a page restored with the box already ticked enables its button on show", () => {
        render(`<form><input type="checkbox" id="tos" checked><button type="submit" data-enabled-by="tos" disabled>Go</button></form>`);
        window.dispatchEvent(new Event("pageshow"));
        expect(document.querySelector("button")?.disabled).toBe(false);
    });
});

describe("data-reveal", () => {
    const markup = (rowHidden: boolean) => `
      <form>
        <button type="button" class="btn" id="toggle" data-reveal="dates"${rowHidden ? "" : " hidden"}><i>event</i> Add dates</button>
        <div id="dates"${rowHidden ? " hidden" : ""}><input id="start" type="date"><input id="end" type="date"></div>
      </form>`;
    const el = (id: string): HTMLElement => {
        const found = document.getElementById(id);
        if (!found) throw new Error(id);
        return found;
    };

    test("shows the section, hides its button and puts the cursor in it; resetting the form undoes that", () => {
        const form = render(markup(true));
        el("toggle").querySelector("i")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect([el("dates").hidden, el("toggle").hidden, document.activeElement?.id]).toEqual([false, true, "start"]);
        form.reset();
        expect([el("dates").hidden, el("toggle").hidden]).toEqual([true, false]);
    });

    test("a section the server rendered open stays open on reset", () => {
        const form = render(markup(false));
        form.reset();
        expect([el("dates").hidden, el("toggle").hidden]).toEqual([false, true]);
    });
});

describe("data-navigate", () => {
    test("choosing an option goes to the address it carries", () => {
        const went: string[] = [];
        const realLocation = window.location;
        Object.defineProperty(window, "location", { value: { assign: (url: string) => void went.push(url) }, configurable: true });
        try {
            document.body.innerHTML = `<select data-navigate><option value="/trips/?sort=updated&amp;dir=desc" selected>New</option><option value="/trips/?sort=start_date&amp;dir=asc">Soonest</option></select>`;
            const select = document.querySelector("select");
            if (!select) throw new Error("select");
            select.value = "/trips/?sort=start_date&dir=asc";
            select.dispatchEvent(new Event("change", { bubbles: true }));
        } finally {
            Object.defineProperty(window, "location", { value: realLocation, configurable: true });
        }
        expect(went).toEqual(["/trips/?sort=start_date&dir=asc"]);
    });
});

describe("data-placeholder-ideas", () => {
    test("each time its dialog closes the field suggests another idea", () => {
        document.body.innerHTML = `
          <dialog id="d"><input id="name" placeholder="e.g. First" data-placeholder-ideas="ideas"></dialog>
          <script type="application/json" id="ideas">["Millpond Ramble"]</script>`;
        document.getElementById("d")?.dispatchEvent(new Event("close"));
        expect(document.getElementById("name")?.getAttribute("placeholder")).toBe("e.g. Millpond Ramble");
    });
});
