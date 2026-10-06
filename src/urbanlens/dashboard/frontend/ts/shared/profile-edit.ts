/**
 * The profile edit page (``pages/profile/edit.html``): every ``[data-autosave]`` field saves on change, the
 * username is checked as it is typed, and the avatar choices save at once and show everywhere the page draws the
 * user's avatar (``data-user-avatar``). Values arrive on ``.edit-profile-page``'s data attributes.
 */

import { saveAvatar, saveProfileField, showSavedAvatar, USERNAME_PATTERN, USERNAME_RULE, usernameAvailability, avatarSavedText, type AvatarChoice, type FieldResult } from "./profile-field";

const DATE_FIELDS = new Set(["birth_date", "started_exploring"]);
const USERNAME_CHECK_DELAY_MS = 400;
const SAVED_BADGE_MS = 2500;

function byId(id: string): HTMLElement | null {
    return document.getElementById(id);
}

function dateDialog(): HTMLDialogElement | null {
    const el = byId("date-error-dialog");
    return el instanceof HTMLDialogElement ? el : null;
}

/** Open the invalid-date dialog with *message* as its one reason. */
export function showDateError(message: string): void {
    const dialog = dateDialog();
    const list = dialog?.querySelector(".date-error-list");
    if (!dialog || !list) return;
    const item = document.createElement("li");
    item.textContent = message;
    list.replaceChildren(item);
    if (!dialog.open) dialog.showModal();
}

export class ProfileEditForm {
    private usernameTimer: number | undefined;
    private lastCheckedUsername = "";

    constructor(private readonly root: HTMLElement) {}

    private get saveUrl(): string {
        return this.root.dataset.saveUrl ?? "";
    }

    install(): void {
        if (dateDialog()?.hasAttribute("data-open-on-load")) dateDialog()?.showModal();
        this.root.addEventListener("change", (event) => this.onChange(event));
        this.root.addEventListener("click", (event) => this.onClick(event));
        const username = byId("username-input");
        if (username instanceof HTMLInputElement) {
            this.showUsernameRule(username.value.trim());
            username.addEventListener("input", () => {
                const value = username.value.trim();
                this.showUsernameRule(value);
                window.clearTimeout(this.usernameTimer);
                this.usernameTimer = window.setTimeout(() => void this.checkUsername(value), USERNAME_CHECK_DELAY_MS);
            });
            username.addEventListener("blur", () => {
                const value = username.value.trim();
                if (USERNAME_PATTERN.test(value)) void this.save("username", value);
            });
        }
    }

    private onChange(event: Event): void {
        const el = event.target;
        if (el instanceof HTMLSelectElement && el.dataset.preferenceToggle) {
            const other = this.root.querySelector<HTMLElement>(`[data-preference-other-for="${CSS.escape(el.dataset.preferenceToggle)}"]`);
            if (other) other.style.display = el.value === "other" ? "" : "none";
        }
        if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement)) return;
        const field = el.dataset.autosave;
        if (!field) return;
        if (el instanceof HTMLInputElement && el.type === "file") {
            const file = el.files?.[0];
            if (file) void this.chooseAvatar({ kind: "upload", file });
            return;
        }
        void this.save(field, el.value);
    }

    private onClick(event: MouseEvent): void {
        const target = event.target instanceof Element ? event.target : null;
        const gravatar = target?.closest<HTMLElement>("#avatar-gravatar-btn");
        if (gravatar) {
            void this.chooseAvatar({ kind: "gravatar", previewUrl: gravatar.dataset.gravatarUrl });
            return;
        }
        const emoji = target?.closest<HTMLElement>(".edit-avatar-emoji-opt");
        if (emoji) {
            void this.chooseAvatar({ kind: "emoji", animal: emoji.dataset.animal ?? "", color: emoji.dataset.color ?? "" });
            return;
        }
        if (target?.closest("#setup-skip-btn")) void this.skipSetup();
    }

    private setStatus(field: string, state: "" | "saving" | "saved" | "error", text: string): void {
        const el = this.root.querySelector(`[data-field-status="${CSS.escape(field)}"]`);
        if (!el) return;
        el.className = `field-status${state ? ` field-status--${state}` : ""}`;
        el.textContent = text;
        if (state === "saved") window.setTimeout(() => this.setStatus(field, "", ""), SAVED_BADGE_MS);
    }

    private async save(field: string, value: string): Promise<void> {
        const guard = window.autosaveGuard;
        guard?.markDirty();
        this.setStatus(field, "saving", "Saving...");
        guard?.saveStarted();
        try {
            const result = await saveProfileField(this.saveUrl, field, value);
            if (result.ok) {
                guard?.markClean();
                this.setStatus(field, "saved", "✓ Saved");
            } else if (result.refused) {
                this.refused(field, result.error || "Save failed.");
            } else {
                this.setStatus(field, "error", "Save failed.");
                if (result.error) window.toastr?.error(result.error);
            }
        } finally {
            guard?.saveFinished();
        }
    }

    private refused(field: string, message: string): void {
        const input = this.root.querySelector(`[data-autosave="${CSS.escape(field)}"]`);
        if ((input instanceof HTMLInputElement && input.type !== "file") || input instanceof HTMLTextAreaElement) input.value = "";
        this.setStatus(field, "error", "✗");
        window.toastr?.error(message);
        if (DATE_FIELDS.has(field)) showDateError(message);
    }

    private async chooseAvatar(choice: AvatarChoice): Promise<void> {
        this.setStatus("avatar", "saving", "Saving...");
        const result: FieldResult = await saveAvatar(this.saveUrl, choice);
        if (!result.ok) {
            this.setStatus("avatar", "error", result.refused ? "✗" : "Failed");
            if (result.refused || result.error) window.toastr?.error(result.error || "Could not save avatar.");
            return;
        }
        this.setStatus("avatar", "saved", avatarSavedText(result));
        const outcome = await showSavedAvatar(this.saveUrl, choice, result, { mediaOrigin: this.root.dataset.mediaOrigin });
        if (outcome === "published") this.setStatus("avatar", "saved", "✓ Saved");
        if (outcome === "unchanged") {
            this.setStatus("avatar", "error", "✗");
            window.toastr?.error("That image couldn't be processed, so your picture hasn't changed.");
        }
    }

    // -- Username

    private showUsernameRule(username: string): void {
        const rule = byId("username-requirements-hint");
        if (rule) rule.style.display = USERNAME_PATTERN.test(username) ? "none" : "";
    }

    private usernameHint(text: string, ok: boolean): void {
        const hint = byId("username-hint");
        if (!hint) return;
        hint.className = `edit-username-hint edit-username-hint--${ok ? "ok" : "bad"}`;
        hint.textContent = text;
    }

    private async checkUsername(username: string): Promise<void> {
        if (!username || username === this.lastCheckedUsername) return;
        this.lastCheckedUsername = username;
        if (!USERNAME_PATTERN.test(username)) {
            this.usernameHint(USERNAME_RULE, false);
            return;
        }
        const hint = byId("username-hint");
        if (hint) hint.textContent = "";
        const result = await usernameAvailability(this.saveUrl, username);
        if (result) this.usernameHint(result.available ? "✓ Available" : result.reason, result.available);
    }

    // -- Setup banner

    private async skipSetup(): Promise<void> {
        await saveProfileField(this.saveUrl, "setup_complete");
        window.location.href = this.root.dataset.mapUrl ?? "/";
    }
}
