/**
 * The first-run setup wizard (``pages/setup/index.html``): five steps, the profile's username and avatar, and
 * the site's title, saved as it is typed. Values arrive on ``.setup-wizard``'s data attributes.
 */

import { getCsrfToken } from "./csrf";
import { fetchJson } from "./fetch-json";
import { saveAvatar, saveProfileField, showSavedAvatar, USERNAME_PATTERN, USERNAME_RULE, usernameAvailability, avatarSavedText, type AvatarChoice } from "./profile-field";

const TOTAL_STEPS = 5;
/** The step whose title must be valid before the wizard moves past it. */
const TITLE_STEP = 3;
const TITLE_SAVE_DELAY_MS = 900;
const USERNAME_CHECK_DELAY_MS = 400;
const SAVED_BADGE_MS = 2200;

const COLOR_OK = "#43a047";
const COLOR_BAD = "#e53935";
const COLOR_PENDING = "#9aa0a6";

function byId(id: string): HTMLElement | null {
    return document.getElementById(id);
}

export class SetupWizard {
    private step = 1;
    private titleTimer: number | undefined;
    private savedTimer: number | undefined;
    private usernameTimer: number | undefined;
    private lastCheckedUsername = "";

    constructor(private readonly root: HTMLElement) {}

    private get cfg(): DOMStringMap {
        return this.root.dataset;
    }

    private titleInput(): HTMLInputElement | null {
        const el = byId("app-title-input");
        return el instanceof HTMLInputElement ? el : null;
    }

    install(): void {
        this.root.addEventListener("click", (event) => this.onClick(event));
        const title = this.titleInput();
        if (title) {
            title.addEventListener("input", () => this.onTitleInput(title));
        }
        const username = byId("setup-username-input");
        if (username instanceof HTMLInputElement) {
            username.addEventListener("input", () => {
                window.clearTimeout(this.usernameTimer);
                const value = username.value.trim();
                this.usernameTimer = window.setTimeout(() => void this.checkUsername(value), USERNAME_CHECK_DELAY_MS);
            });
            username.addEventListener("blur", () => void this.saveUsername(username.value.trim()));
        }
        const upload = byId("setup-avatar-upload");
        if (upload instanceof HTMLInputElement) {
            upload.addEventListener("change", () => {
                const file = upload.files?.[0];
                if (file) void this.chooseAvatar({ kind: "upload", file });
            });
        }
    }

    private onClick(event: MouseEvent): void {
        const target = event.target instanceof Element ? event.target : null;
        const goto = target?.closest<HTMLElement>("[data-setup-goto]");
        if (goto) {
            this.goToStep(Number(goto.dataset.setupGoto));
            return;
        }
        const gravatar = target?.closest<HTMLElement>("#setup-avatar-gravatar-btn");
        if (gravatar) {
            void this.chooseAvatar({ kind: "gravatar", previewUrl: gravatar.dataset.gravatarUrl });
            return;
        }
        const emoji = target?.closest<HTMLElement>(".setup-avatar-emoji-btn");
        if (emoji) void this.chooseAvatar({ kind: "emoji", animal: emoji.dataset.animal ?? "", color: emoji.dataset.color ?? "" });
    }

    // -- Steps

    goToStep(n: number): void {
        if (!Number.isInteger(n) || n < 1 || n > TOTAL_STEPS) return;
        const title = this.titleInput();
        if (n > this.step && this.step === TITLE_STEP && title) {
            const value = title.value.trim();
            if (!value) {
                title.focus();
                return;
            }
        }
        this.step = n;
        this.root.querySelectorAll<HTMLElement>(".setup-step").forEach((el) => el.classList.toggle("setup-step--active", Number(el.dataset.step) === n));
        this.root.querySelectorAll<HTMLElement>(".setup-stepper__item").forEach((item) => {
            const sn = Number(item.dataset.step);
            item.classList.toggle("setup-stepper__item--active", sn === n);
            item.classList.toggle("setup-stepper__item--done", sn < n);
            const num = item.querySelector(".step-num, .material-icons");
            if (!num) return;
            num.className = sn < n ? "material-icons" : "step-num";
            num.textContent = sn < n ? "check" : String(sn);
        });
        const progress = byId("setup-progress");
        if (progress) progress.style.width = `${(n / TOTAL_STEPS) * 100}%`;
    }

    // -- Site title

    private onTitleInput(input: HTMLInputElement): void {
        const shown = input.value.trim() || "UrbanLens";
        for (const id of ["title-preview-value", "sidebar-title", "done-app-title"]) {
            const el = byId(id);
            if (el) el.textContent = shown;
        }
        window.autosaveGuard?.markDirty();
        window.clearTimeout(this.titleTimer);
        this.titleTimer = window.setTimeout(() => void this.saveTitle(), TITLE_SAVE_DELAY_MS);
    }

    private async saveTitle(): Promise<void> {
        const value = this.titleInput()?.value.trim() ?? "";
        if (!value) return;
        const guard = window.autosaveGuard;
        guard?.saveStarted();
        try {
            await fetchJson(this.cfg.setupUrl ?? "", {
                method: "POST",
                headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/x-www-form-urlencoded" },
                body: new URLSearchParams({ action: "save_title", app_title: value }),
                reportsItsOwnErrors: true,
            });
            guard?.markClean();
            const status = byId("title-save-status");
            if (status) {
                status.classList.add("setup-save-status--visible");
                window.clearTimeout(this.savedTimer);
                this.savedTimer = window.setTimeout(() => status.classList.remove("setup-save-status--visible"), SAVED_BADGE_MS);
            }
        } catch {
            // Not reported here: the next edit saves again.
        } finally {
            guard?.saveFinished();
        }
    }

    // -- Username and avatar

    private usernameHint(text: string, color: string): void {
        const hint = byId("setup-username-hint");
        if (!hint) return;
        hint.style.color = color;
        hint.textContent = text;
    }

    private async checkUsername(username: string): Promise<void> {
        if (!username || username === this.lastCheckedUsername) return;
        this.lastCheckedUsername = username;
        if (!USERNAME_PATTERN.test(username)) {
            this.usernameHint(USERNAME_RULE, COLOR_BAD);
            return;
        }
        this.usernameHint("", COLOR_PENDING);
        const result = await usernameAvailability(this.cfg.profileUrl ?? "", username);
        if (result) this.usernameHint(result.available ? "✓ Available" : result.reason, result.available ? COLOR_OK : COLOR_BAD);
    }

    private async saveUsername(username: string): Promise<void> {
        if (!USERNAME_PATTERN.test(username)) return;
        const result = await saveProfileField(this.cfg.profileUrl ?? "", "username", username);
        this.usernameHint(result.ok ? "✓ Saved" : result.error || "Save failed", result.ok ? COLOR_OK : COLOR_BAD);
    }

    private avatarStatus(text: string, color: string): void {
        const status = byId("setup-avatar-status");
        if (!status) return;
        status.textContent = text;
        status.style.color = color;
    }

    private async chooseAvatar(choice: AvatarChoice): Promise<void> {
        this.avatarStatus("Saving...", COLOR_PENDING);
        const result = await saveAvatar(this.cfg.profileUrl ?? "", choice);
        if (!result.ok) {
            this.avatarStatus(`✗ ${result.error || "Failed"}`, COLOR_BAD);
            return;
        }
        this.avatarStatus(avatarSavedText(result), COLOR_OK);
        const outcome = await showSavedAvatar(this.cfg.profileUrl ?? "", choice, result, { mediaOrigin: this.cfg.mediaOrigin });
        if (outcome === "published") this.avatarStatus("✓ Saved", COLOR_OK);
        if (outcome === "unchanged") this.avatarStatus("✗ That image couldn't be processed.", COLOR_BAD);
    }
}
