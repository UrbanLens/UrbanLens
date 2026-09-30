import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { isReservedTitle, SetupWizard } from "./setup-wizard";

interface Call {
    url: string;
    method: string;
    body: string;
}

const realFetch = globalThis.fetch;
let calls: Call[] = [];
let respond: (url: string) => Response = () => new Response(JSON.stringify({ ok: true }), { status: 200 });

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function render(official = false): SetupWizard {
    const config = official ? 'data-official="true"' : 'data-title-notice="Pick another name."';
    document.body.innerHTML = `
      <div class="setup-wizard" data-setup-url="/setup/" data-profile-url="/profile/field/" data-suggested-title="My Map" ${config}>
        <span id="sidebar-title">My Map</span>
        <ul>${[1, 2, 3, 4, 5].map((n) => `<li class="setup-stepper__item" data-step="${n}"><span class="step-num">${n}</span></li>`).join("")}</ul>
        <div id="setup-progress"></div>
        ${[1, 2, 3, 4, 5].map((n) => `<div class="setup-step${n === 1 ? " setup-step--active" : ""}" data-step="${n}"><button type="button" data-setup-goto="${n + 1}">Next</button></div>`).join("")}
        <input id="setup-username-input">
        <span id="setup-username-hint"></span>
        <div id="setup-avatar-preview">JM</div>
        <span id="setup-avatar-status"></span>
        <button type="button" id="setup-avatar-gravatar-btn">Gravatar</button>
        <button type="button" class="setup-avatar-emoji-btn" data-animal="owl" data-color="teal">Owl</button>
        <input id="app-title-input" value="">
        <button type="button" class="setup-title-suggestion-btn" data-title="Rust Belt Atlas">Suggest</button>
        <span id="title-preview-value"></span>
        <strong id="done-app-title"></strong>
        <div id="title-reserved-notice" hidden><p id="title-reserved-message"></p></div>
        <span id="title-save-status"></span>
      </div>`;
    const root = document.querySelector<HTMLElement>(".setup-wizard");
    if (!root) throw new Error("no wizard");
    const wizard = new SetupWizard(root);
    wizard.install();
    return wizard;
}

function activeStep(): string | undefined {
    return document.querySelector<HTMLElement>(".setup-step--active")?.dataset.step;
}

function titleInput(): HTMLInputElement {
    const el = document.getElementById("app-title-input");
    if (!(el instanceof HTMLInputElement)) throw new Error("no title input");
    return el;
}

beforeAll(() => {
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body instanceof FormData ? JSON.stringify(Object.fromEntries(init.body)) : String(init?.body ?? "");
        calls.push({ url, method: init?.method ?? "GET", body });
        return respond(url);
    }, realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
});

beforeEach(() => {
    calls = [];
    respond = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
});

describe("isReservedTitle", () => {
    test("catches the name however it is spaced or cased", () => {
        expect(isReservedTitle("Urban Lens")).toBe(true);
        expect(isReservedTitle("urban-LENS!")).toBe(true);
        expect(isReservedTitle("UrbanLens Rochester")).toBe(false);
    });
});

describe("steps", () => {
    test("Next moves on, and a finished step shows a check that Back takes away", () => {
        render();
        document.querySelector<HTMLElement>('[data-setup-goto="2"]')?.click();
        expect(activeStep()).toBe("2");
        const first = document.querySelector('.setup-stepper__item[data-step="1"] > span');
        expect(first?.textContent).toBe("check");
        const wizard = render();
        wizard.goToStep(3);
        wizard.goToStep(1);
        expect(document.querySelector('.setup-stepper__item[data-step="2"] > span')?.textContent).toBe("2");
        expect(document.getElementById("setup-progress")?.style.width).toBe("20%");
    });

    test("the title step holds until there is a title", () => {
        const wizard = render();
        wizard.goToStep(3);
        wizard.goToStep(4);
        expect(activeStep()).toBe("3");
        titleInput().value = "Rust Belt Atlas";
        wizard.goToStep(4);
        expect(activeStep()).toBe("4");
    });

    test("an unofficial site cannot pass the title step as UrbanLens", () => {
        const wizard = render();
        wizard.goToStep(3);
        titleInput().value = "Urban Lens";
        wizard.goToStep(4);
        expect(activeStep()).toBe("3");
        expect(document.getElementById("title-reserved-notice")?.hidden).toBe(false);
        expect(document.getElementById("title-reserved-message")?.textContent).toBe("Pick another name.");
    });

    test("the official site may call itself UrbanLens", () => {
        const wizard = render(true);
        wizard.goToStep(3);
        titleInput().value = "UrbanLens";
        wizard.goToStep(4);
        expect(activeStep()).toBe("4");
    });
});

describe("title", () => {
    // Each test's pending save would otherwise fire into the next one's page.
    afterEach(() => settle(1000));

    test("a suggestion fills the title and every preview of it", () => {
        render();
        document.querySelector<HTMLElement>(".setup-title-suggestion-btn")?.click();
        expect(titleInput().value).toBe("Rust Belt Atlas");
        for (const id of ["title-preview-value", "sidebar-title", "done-app-title"]) expect(document.getElementById(id)?.textContent).toBe("Rust Belt Atlas");
    });

    test("an emptied title previews the suggested one", () => {
        render();
        titleInput().dispatchEvent(new Event("input"));
        expect(document.getElementById("sidebar-title")?.textContent).toBe("My Map");
    });

    test("typing saves the title once, after a pause", async () => {
        respond = () => new Response(null, { status: 204 });
        render();
        const input = titleInput();
        input.value = "Rust";
        input.dispatchEvent(new Event("input"));
        input.value = "Rust Belt";
        input.dispatchEvent(new Event("input"));
        await settle(1000);
        expect(calls).toEqual([{ url: "/setup/", method: "POST", body: "action=save_title&app_title=Rust+Belt" }]);
    });

    test("a title the server refuses shows the server's reason", async () => {
        respond = () => new Response(JSON.stringify({ error: "That name is taken by the public site." }), { status: 400 });
        render();
        const input = titleInput();
        input.value = "Rust Belt";
        input.dispatchEvent(new Event("input"));
        await settle(1000);
        expect(document.getElementById("title-reserved-notice")?.hidden).toBe(false);
        expect(document.getElementById("title-reserved-message")?.textContent).toBe("That name is taken by the public site.");
    });
});

describe("profile", () => {
    test("a malformed username is explained without asking the server", async () => {
        render();
        const input = document.getElementById("setup-username-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no username input");
        input.value = "ab";
        input.dispatchEvent(new Event("input"));
        await settle(450);
        expect(calls).toEqual([]);
        expect(document.getElementById("setup-username-hint")?.textContent).toContain("3-30 chars");
    });

    test("a refused username keeps the server's reason", async () => {
        respond = () => new Response(JSON.stringify({ ok: false, error: "That username is taken." }), { status: 200 });
        render();
        const input = document.getElementById("setup-username-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no username input");
        input.value = "urbex_owl";
        input.dispatchEvent(new Event("blur"));
        await settle();
        expect(JSON.parse(calls[0]?.body ?? "{}")).toEqual({ field: "username", value: "urbex_owl" });
        expect(document.getElementById("setup-username-hint")?.textContent).toBe("That username is taken.");
    });

    test("an emoji avatar replaces the initials with the saved image", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "/media/avatars/owl.png" }), { status: 200 });
        render();
        document.querySelector<HTMLElement>(".setup-avatar-emoji-btn")?.click();
        await settle();
        expect(JSON.parse(calls[0]?.body ?? "{}")).toEqual({ field: "avatar_emoji", animal: "owl", color: "teal" });
        const preview = document.getElementById("setup-avatar-preview");
        expect(preview instanceof HTMLImageElement).toBe(true);
        expect(preview?.className).toBe("setup-avatar-preview");
        expect(preview?.getAttribute("src")).toStartWith("/media/avatars/owl.png?");
        expect(document.getElementById("setup-avatar-status")?.textContent).toBe("✓ Saved");
    });

    test("a failed avatar save says why", async () => {
        respond = () => new Response(JSON.stringify({ error: "Gravatar is unreachable." }), { status: 502 });
        render();
        document.getElementById("setup-avatar-gravatar-btn")?.click();
        await settle();
        expect(document.getElementById("setup-avatar-status")?.textContent).toBe("✗ Gravatar is unreachable.");
        expect(document.getElementById("setup-avatar-preview") instanceof HTMLImageElement).toBe(false);
    });
});
