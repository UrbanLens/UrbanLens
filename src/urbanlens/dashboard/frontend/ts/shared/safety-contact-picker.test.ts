import { beforeEach, describe, expect, test } from "bun:test";

import { initContactPickers } from "./safety-contact-picker";

let changes = 0;

function render(opts: { locked?: boolean } = {}): HTMLElement {
    document.body.innerHTML = `
      <form id="f">
        <div class="safety-contact-picker card safety-contact-picker--collapsible" data-role="contact-picker">
          <button type="button" data-role="edit-toggle" aria-label="Add emergency contacts"><i class="material-symbols-outlined">add</i></button>
          <div data-role="chips">
            <span class="safety-contact-chip" data-type="profile" data-id="7">
              <span class="safety-contact-chip-label">owl</span>
              <input type="hidden" name="contact_profile_ids" value="7">
              ${opts.locked ? "" : '<button type="button" class="safety-contact-chip-remove" data-role="remove"><i>close</i></button>'}
            </span>
          </div>
          ${
              opts.locked
                  ? ""
                  : `<div data-role="avatar-picker">
            <button type="button" data-role="avatar-btn" data-id="7" data-username="owl">owl</button>
            <button type="button" data-role="avatar-btn" data-id="8" data-username="fox"><img src="/media/fox.png" alt=""></button>
          </div>
          <script type="application/json">[{"id": 7, "username": "owl"}, {"id": 8, "username": "fox", "full_name": "Fern Fox"}, {"id": 9, "username": "bat", "avatar_url": "javascript:alert(1)"}]</script>
          <input type="text" data-role="input">
          <div data-role="suggestions" hidden></div>`
          }
        </div>
      </form>`;
    document.getElementById("f")?.addEventListener("contactschange", () => void changes++);
    initContactPickers();
    initContactPickers();
    const root = document.querySelector<HTMLElement>('[data-role="contact-picker"]');
    if (!root) throw new Error("no picker");
    return root;
}

function input(): HTMLInputElement {
    const el = document.querySelector('[data-role="input"]');
    if (!(el instanceof HTMLInputElement)) throw new Error("no input");
    return el;
}

function submitted(): Record<string, string[]> {
    const form = document.getElementById("f");
    if (!(form instanceof HTMLFormElement)) throw new Error("no form");
    const data = new FormData(form);
    return { ids: data.getAll("contact_profile_ids").map(String), emails: data.getAll("contact_emails").map(String) };
}

function key(k: string): KeyboardEvent {
    const event = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
    input().dispatchEvent(event);
    return event;
}

beforeEach(() => {
    changes = 0;
});

describe("chips", () => {
    test("a friend already added is hidden from the quick picks, and back once removed", () => {
        render();
        const owl = document.querySelector<HTMLElement>('[data-role="avatar-btn"][data-id="7"]');
        expect(owl?.hidden).toBe(true);
        document.querySelector<HTMLElement>('[data-role="remove"]')?.click();
        expect(owl?.hidden).toBe(false);
        expect(submitted().ids).toEqual([]);
        expect(changes).toBe(1);
    });

    test("a quick pick adds a chip the form submits, once", () => {
        render();
        const fox = document.querySelector<HTMLElement>('[data-role="avatar-btn"][data-id="8"]');
        fox?.click();
        fox?.click();
        expect(submitted().ids).toEqual(["7", "8"]);
        expect(document.querySelector('[data-type="profile"][data-id="8"] img')?.getAttribute("src")).toEndWith("/media/fox.png");
        expect(changes).toBe(1);
    });

    test("an email typed then left is still added", () => {
        render();
        input().value = "Jane@Example.com";
        input().dispatchEvent(new Event("blur"));
        expect(submitted().emails).toEqual(["jane@example.com"]);
        expect(input().value).toBe("");
    });

    test("Enter adds an exact friend; a space after an email moves on; a space in a name stays", () => {
        render();
        input().value = "FOX";
        expect(key("Enter").defaultPrevented).toBe(true);
        input().value = "a@b.co";
        expect(key(" ").defaultPrevented).toBe(true);
        input().value = "Fern";
        expect(key(" ").defaultPrevented).toBe(false);
        expect(submitted()).toEqual({ ids: ["7", "8"], emails: ["a@b.co"] });
    });

    test("leaving for the submit button doesn't add the chip then, so the button stays under the click", () => {
        render();
        const submit = document.createElement("button");
        submit.type = "submit";
        document.getElementById("f")?.append(submit);
        input().value = "jane@example.com";
        input().dispatchEvent(new FocusEvent("blur", { relatedTarget: submit }));
        expect(submitted().emails).toEqual([]);
    });

    test("an email still in the box when the form submits goes with it", () => {
        render();
        input().value = "jane@example.com";
        document.getElementById("f")?.dispatchEvent(new Event("submit", { cancelable: true }));
        expect(submitted().emails).toEqual(["jane@example.com"]);
    });

    test("something that is neither a friend nor an email is left in the box", () => {
        render();
        input().value = "nobody";
        key("Enter");
        expect(input().value).toBe("nobody");
        expect(changes).toBe(0);
    });

    test("a locked picker has nothing to remove or add", () => {
        render({ locked: true });
        expect(document.querySelector('[data-role="remove"]')).toBeNull();
        expect(submitted().ids).toEqual(["7"]);
    });
});

describe("suggestions", () => {
    test("match by username or full name, leaving out friends already added", () => {
        render();
        input().value = "f";
        input().dispatchEvent(new Event("input"));
        const labels = Array.from(document.querySelectorAll(".safety-contact-suggestion-label"), (el) => el.textContent);
        expect(labels).toEqual(["fox (Fern Fox)"]);
        document.querySelector<HTMLElement>(".safety-contact-suggestion")?.click();
        expect(submitted().ids).toEqual(["7", "8"]);
        expect(document.querySelector<HTMLElement>('[data-role="suggestions"]')?.hidden).toBe(true);
    });

    test("a friend's avatar that isn't an http(s) URL shows their initial instead", () => {
        render();
        input().value = "bat";
        input().dispatchEvent(new Event("input"));
        const face = document.querySelector(".safety-contact-suggestion-avatar");
        expect(face?.tagName).toBe("SPAN");
        expect(face?.textContent).toBe("B");
    });
});

describe("edit toggle", () => {
    test("opens and closes the collapsible picker", () => {
        const root = render();
        const toggle = document.querySelector<HTMLElement>('[data-role="edit-toggle"]');
        toggle?.click();
        expect(root.classList.contains("is-editing")).toBe(true);
        expect(toggle?.getAttribute("aria-label")).toBe("Done editing emergency contacts");
        toggle?.click();
        expect(toggle?.textContent?.trim()).toBe("add");
    });
});
