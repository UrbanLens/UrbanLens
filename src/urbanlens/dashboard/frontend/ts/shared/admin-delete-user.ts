/**
 * Site admin > Users (``pages/site_admin_users.html``): a row's Delete opens the shared delete dialog for that user.
 * The row carries the user's id, the name to show and the phrase the server will expect typed back; the dialog's
 * button is gated on that phrase by ``data-enabled-by`` (``declarative-actions.ts``).
 */

function field<T extends HTMLElement>(root: Document, id: string, type: new () => T): T | null {
    const el = root.getElementById(id);
    return el instanceof type ? el : null;
}

/** Returns the uninstaller. */
export function installAdminDeleteUser(root: Document): () => void {
    const onClick = (event: MouseEvent): void => {
        const row = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-admin-delete-user]") : null;
        const dialog = field(root, "admin-delete-user-dialog", HTMLDialogElement);
        const input = field(root, "admin-delete-user-confirm-input", HTMLInputElement);
        const id = field(root, "admin-delete-user-id", HTMLInputElement);
        if (!row || !dialog || !input || !id) return;
        const expected = row.dataset.expect ?? "";
        id.value = row.dataset.adminDeleteUser ?? "";
        const name = field(root, "admin-delete-user-name", HTMLElement);
        if (name) name.textContent = row.dataset.name ?? "";
        const label = field(root, "admin-delete-user-confirm-label", HTMLElement);
        if (label) label.textContent = `Type "${expected}" to confirm`;
        input.value = "";
        input.placeholder = expected;
        input.dataset.expect = expected;
        const confirm = field(root, "admin-delete-user-confirm-btn", HTMLButtonElement);
        if (confirm) confirm.disabled = true;
        dialog.showModal();
        input.focus();
    };
    root.addEventListener("click", onClick);
    return () => root.removeEventListener("click", onClick);
}
