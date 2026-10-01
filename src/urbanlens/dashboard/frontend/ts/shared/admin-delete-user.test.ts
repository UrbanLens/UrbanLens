import { afterAll, beforeAll, expect, test } from "bun:test";

import { installAdminDeleteUser } from "./admin-delete-user";

const realShowModal = HTMLDialogElement.prototype.showModal;
let uninstall: () => void = () => {};

beforeAll(() => {
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
        this.setAttribute("open", "");
    };
    document.body.innerHTML = `
      <button type="button" data-admin-delete-user="7" data-name="ada" data-expect="ada"><i>delete_forever</i> Delete</button>
      <button type="button" data-admin-delete-user="9" data-name="this hidden user" data-expect="hidden user">Delete</button>
      <dialog id="admin-delete-user-dialog">
        <form>
          <input type="hidden" name="user_id" id="admin-delete-user-id">
          <strong id="admin-delete-user-name"></strong>
          <span id="admin-delete-user-confirm-label"></span>
          <input type="text" name="confirm_text" id="admin-delete-user-confirm-input">
          <button type="submit" id="admin-delete-user-confirm-btn" data-enabled-by="admin-delete-user-confirm-input" disabled>Delete</button>
        </form>
      </dialog>`;
    uninstall = installAdminDeleteUser(document);
});

afterAll(() => {
    uninstall();
    HTMLDialogElement.prototype.showModal = realShowModal;
});

const el = <T extends HTMLElement>(id: string, type: new () => T): T => {
    const found = document.getElementById(id);
    if (!(found instanceof type)) throw new Error(id);
    return found;
};

test("a row's Delete opens the dialog for that user, asking for that user's phrase", () => {
    document.querySelector('[data-admin-delete-user="7"] i')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = el("admin-delete-user-confirm-input", HTMLInputElement);
    expect(el("admin-delete-user-dialog", HTMLDialogElement).open).toBe(true);
    expect(el("admin-delete-user-id", HTMLInputElement).value).toBe("7");
    expect(el("admin-delete-user-name", HTMLElement).textContent).toBe("ada");
    expect(el("admin-delete-user-confirm-label", HTMLElement).textContent).toBe('Type "ada" to confirm');
    expect([input.placeholder, input.dataset.expect, document.activeElement]).toEqual(["ada", "ada", input]);
});

test("opening it for another user starts over", () => {
    const input = el("admin-delete-user-confirm-input", HTMLInputElement);
    const button = el("admin-delete-user-confirm-btn", HTMLButtonElement);
    input.value = "ada";
    button.disabled = false;
    el("admin-delete-user-dialog", HTMLDialogElement).removeAttribute("open");
    document.querySelector<HTMLElement>('[data-admin-delete-user="9"]')?.click();
    expect([el("admin-delete-user-id", HTMLInputElement).value, input.value, input.dataset.expect, button.disabled]).toEqual(["9", "", "hidden user", true]);
    expect(el("admin-delete-user-name", HTMLElement).textContent).toBe("this hidden user");
});
