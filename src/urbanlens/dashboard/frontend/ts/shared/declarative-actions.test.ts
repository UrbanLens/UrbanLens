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

describe("data-no-submit", () => {
    test("a form whose fields a script reads never submits itself", () => {
        render(`<form data-no-submit><input name="q" value="owl"><button type="submit" name="go">Go</button></form>`);
        document.querySelector("button")?.click();
        expect(submitted).toEqual([]);
    });

    test("a form without it still submits", () => {
        render(`<form><button type="submit" name="go">Go</button></form>`);
        document.querySelector("button")?.click();
        expect(submitted).toEqual(["go"]);
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
    function choose(value: string): string[] {
        const went: string[] = [];
        const realLocation = window.location;
        const fake = { href: "https://urbanlens.test/trips/", origin: "https://urbanlens.test", assign: (url: string) => void went.push(url) };
        Object.defineProperty(window, "location", { value: fake, configurable: true });
        try {
            document.body.innerHTML = `<select data-navigate><option value="/trips/?sort=updated&amp;dir=desc" selected>New</option></select>`;
            const select = document.querySelector("select");
            if (!select) throw new Error("select");
            const option = document.createElement("option");
            option.value = value;
            select.append(option);
            select.value = value;
            select.dispatchEvent(new Event("change", { bubbles: true }));
        } finally {
            Object.defineProperty(window, "location", { value: realLocation, configurable: true });
        }
        return went;
    }

    test("choosing an option goes to the address it carries", () => {
        expect(choose("/trips/?sort=start_date&dir=asc")).toEqual(["/trips/?sort=start_date&dir=asc"]);
    });

    test("an option carrying a script or another site's address goes nowhere", () => {
        expect(choose("javascript:alert(1)")).toEqual([]);
        expect(choose("https://elsewhere.example/")).toEqual([]);
        expect(choose("//elsewhere.example/")).toEqual([]);
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

describe("data-enabled-by, typed confirmation", () => {
    test("every named field must be filled, and one with data-expect must say that phrase", () => {
        render(`<form>
          <input type="password" id="pw">
          <input type="text" id="phrase" data-expect="delete Ada">
          <button type="submit" data-enabled-by="pw phrase" disabled>Delete</button>
        </form>`);
        const pw = document.getElementById("pw");
        const phrase = document.getElementById("phrase");
        const button = document.querySelector("button");
        if (!(pw instanceof HTMLInputElement) || !(phrase instanceof HTMLInputElement) || !button) throw new Error("markup");
        const type = (el: HTMLInputElement, value: string) => {
            el.value = value;
            el.dispatchEvent(new Event("input", { bubbles: true }));
        };
        type(phrase, "  DELETE ada ");
        expect(button.disabled).toBe(true);
        type(pw, "secret");
        expect(button.disabled).toBe(false);
        type(phrase, "delete ad");
        expect(button.disabled).toBe(true);
    });
});

describe("data-enabled-by on a block", () => {
    test("its fields are disabled, and the block dimmed, while the checkbox it names is off", () => {
        render(`<form>
          <input type="checkbox" id="ai">
          <div id="sub" data-enabled-by="ai"><input type="checkbox" name="kinds"><input type="number" name="n"></div>
        </form>`);
        document.getElementById("sub")?.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        const state = () => {
            const sub = document.getElementById("sub");
            return [sub?.classList.contains("is-off"), ...Array.from(sub?.querySelectorAll("input") ?? []).map((i) => i.disabled)];
        };
        expect(state()).toEqual([true, true, true]);
        document.getElementById("ai")?.click();
        expect(state()).toEqual([false, false, false]);
    });
});

describe("data-enabled-by naming a list of choices", () => {
    test("the button waits for a choice in it", () => {
        render(`<form>
          <ul id="targets"><li><label><input type="radio" name="t" value="1">One</label></li><li><label><input type="radio" name="t" value="2">Two</label></li></ul>
          <button type="submit" data-enabled-by="targets" disabled>Merge</button>
        </form>`);
        const button = document.querySelector("button");
        document.querySelector<HTMLInputElement>('input[value="2"]')?.click();
        expect(button?.disabled).toBe(false);
    });
});

describe("data-picks", () => {
    test("a choice fills the hidden field it names and is the one marked pressed", () => {
        render(`<form>
          <div data-picks="color"><button type="button" data-value="#f00" aria-pressed="true"><i>r</i></button><button type="button" data-value="#00f" aria-pressed="false">b</button></div>
          <input type="hidden" id="color" name="color" value="#f00">
        </form>`);
        document.querySelector('[data-value="#00f"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.querySelector<HTMLInputElement>("#color")?.value).toBe("#00f");
        expect(Array.from(document.querySelectorAll("[data-value]")).map((b) => b.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    });
});

describe("data-readout", () => {
    test("a range shows its value where it says", () => {
        render(`<form><span id="pct">100</span><input type="range" min="0" max="100" value="100" data-readout="pct"></form>`);
        const range = document.querySelector<HTMLInputElement>("input");
        if (!range) throw new Error("range");
        range.value = "40";
        range.dispatchEvent(new Event("input", { bubbles: true }));
        expect(document.getElementById("pct")?.textContent).toBe("40");
    });
});

describe("data-toggles", () => {
    test("a button shows and hides the panel it names, and says whether it is open", () => {
        render(`<form><button type="button" data-toggles="panel"><i>group_add</i></button><div id="panel" hidden></div></form>`);
        const button = document.querySelector<HTMLElement>("[data-toggles]");
        const icon = button?.querySelector("i");
        icon?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect([document.getElementById("panel")?.hidden, button?.classList.contains("is-open"), button?.getAttribute("aria-expanded")]).toEqual([false, true, "true"]);
        icon?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect([document.getElementById("panel")?.hidden, button?.classList.contains("is-open"), button?.getAttribute("aria-expanded")]).toEqual([true, false, "false"]);
    });
});

describe("data-autosubmit", () => {
    test("a file input submits its form once a file is chosen, and not when the choice is cleared", () => {
        const form = render(`<form><input type="file" data-autosubmit></form>`);
        let requested = 0;
        form.requestSubmit = () => void requested++;
        const input = form.querySelector("input") as HTMLInputElement;
        let files: File[] = [];
        Object.defineProperty(input, "files", { get: () => files });

        input.dispatchEvent(new Event("change", { bubbles: true }));
        expect(requested).toBe(0);
        files = [new File(["x"], "plan.pdf")];
        input.dispatchEvent(new Event("change", { bubbles: true }));
        expect(requested).toBe(1);
    });
});

describe("data-autogrow", () => {
    function textarea(id: string, scrollHeight: number): HTMLTextAreaElement {
        const el = document.getElementById(id) as HTMLTextAreaElement;
        Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => scrollHeight });
        return el;
    }

    test("a textarea fits its content when it arrives and as it is typed in", () => {
        render(`<form><div id="panel"><textarea id="note" data-autogrow></textarea><textarea id="plain"></textarea></div></form>`);
        const note = textarea("note", 64);
        const plain = textarea("plain", 64);
        document.getElementById("panel")?.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        expect([note.style.height, plain.style.height]).toEqual(["64px", ""]);

        textarea("note", 120).dispatchEvent(new Event("input", { bubbles: true }));
        textarea("plain", 120).dispatchEvent(new Event("input", { bubbles: true }));
        expect([note.style.height, plain.style.height]).toEqual(["120px", ""]);
    });
});

describe("data-removes", () => {
    test("a control removes the element it names", () => {
        render(`<form></form><div id="modal"><div id="screen" data-removes="modal"></div><button type="button" data-removes="modal"><i>x</i></button></div>`);
        document.querySelector("#screen")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.getElementById("modal")).toBeNull();
    });
});

describe("data-empties", () => {
    test("a button empties the element it names", () => {
        render(`<form><button type="button" data-empties="detail"><i>close</i></button></form><div id="detail"><p>diff</p><p>more</p></div>`);
        document.querySelector("[data-empties] i")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.getElementById("detail")?.childNodes.length).toBe(0);
    });
});
