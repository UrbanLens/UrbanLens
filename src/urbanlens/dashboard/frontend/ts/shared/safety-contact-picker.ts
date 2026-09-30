/**
 * The emergency contact picker (``partials/safety/_contact_picker.html``): chips for friends and email addresses,
 * each carrying the hidden input its form submits. A change fires ``contactschange`` on the picker, bubbling, so
 * the form around it can save.
 */

export interface ContactFriend {
    id: number;
    username: string;
    full_name?: string;
    avatar_url?: string;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_SUGGESTIONS = 6;
const SUGGESTIONS_HIDE_DELAY_MS = 150;

/** Show the collapsible picker's toggle as "done editing" or "add". */
export function setEditToggleState(btn: HTMLElement, editing: boolean): void {
    const icon = btn.querySelector(".material-symbols-outlined");
    if (icon) icon.textContent = editing ? "check" : "add";
    const label = editing ? "Done editing emergency contacts" : "Add emergency contacts";
    btn.title = label;
    btn.setAttribute("aria-label", label);
}

function isFriend(value: unknown): value is ContactFriend {
    if (!value || typeof value !== "object") return false;
    return "id" in value && typeof value.id === "number" && "username" in value && typeof value.username === "string";
}

function readFriends(root: HTMLElement): ContactFriend[] {
    const el = root.querySelector('script[type="application/json"]') ?? document.getElementById("safety-contact-friends-data");
    try {
        const parsed: unknown = JSON.parse(el?.textContent ?? "[]");
        return Array.isArray(parsed) ? parsed.filter(isFriend) : [];
    } catch {
        return [];
    }
}

function safeImageUrl(url: string): string | null {
    try {
        const parsed = new URL(url, window.location.origin);
        return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
    } catch {
        return null;
    }
}

function avatar(className: string, url: string | undefined, initial: string): HTMLElement {
    const safe = url ? safeImageUrl(url) : null;
    if (safe) {
        const img = document.createElement("img");
        img.className = className;
        img.src = safe;
        img.alt = "";
        return img;
    }
    const placeholder = document.createElement("span");
    placeholder.className = `${className} ${className}--placeholder`;
    placeholder.textContent = initial;
    return placeholder;
}

function icon(name: string): HTMLElement {
    const i = document.createElement("i");
    i.className = "material-symbols-outlined";
    i.textContent = name;
    return i;
}

function chip(type: "profile" | "email", face: HTMLElement, label: string, inputName: string, inputValue: string): HTMLElement {
    const span = document.createElement("span");
    span.className = "safety-contact-chip";
    span.dataset.type = type;
    const text = document.createElement("span");
    text.className = "safety-contact-chip-label";
    text.textContent = label;
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = inputName;
    hidden.value = inputValue;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "safety-contact-chip-remove";
    remove.dataset.role = "remove";
    remove.setAttribute("aria-label", `Remove ${label}`);
    remove.append(icon("close"));
    span.append(face, text, hidden, remove);
    return span;
}

class ContactPicker {
    private readonly chips: HTMLElement;
    private readonly friends: ContactFriend[];

    constructor(private readonly root: HTMLElement, chips: HTMLElement) {
        this.chips = chips;
        this.friends = readFriends(root);
    }

    private part<T extends HTMLElement>(role: string, type: new () => T): T | null {
        const el = this.root.querySelector(`[data-role="${role}"]`);
        return el instanceof type ? el : null;
    }

    install(): void {
        this.root.addEventListener("click", (event) => this.onClick(event));
        this.syncAvatarButtons();
        const input = this.part("input", HTMLInputElement);
        if (!input) return;
        input.addEventListener("input", () => this.renderSuggestions(input));
        input.addEventListener("keydown", (event) => this.onKeydown(event, input));
        input.addEventListener("blur", (event) => {
            window.setTimeout(() => this.hideSuggestions(), SUGGESTIONS_HIDE_DELAY_MS);
            // A value typed and then left still counts. Heading for the submit button, adding the chip now would
            // push the button out from under the click, so the submit adds it instead.
            const next = event.relatedTarget;
            const toSubmit = (next instanceof HTMLButtonElement || next instanceof HTMLInputElement) && next.type === "submit" && next.form === input.form;
            if (!toSubmit) this.commit(input);
        });
        input.form?.addEventListener(
            "submit",
            () => {
                // The check-in page re-renders its picker; a replaced one has nothing to add.
                if (input.isConnected) this.commit(input);
            },
            true,
        );
    }

    private onClick(event: MouseEvent): void {
        const target = event.target instanceof Element ? event.target : null;
        const toggle = target?.closest<HTMLElement>('[data-role="edit-toggle"]');
        if (toggle) {
            setEditToggleState(toggle, this.root.classList.toggle("is-editing"));
            return;
        }
        const remove = target?.closest('[data-role="remove"]');
        if (remove) {
            remove.closest(".safety-contact-chip")?.remove();
            this.syncAvatarButtons();
            this.changed();
            return;
        }
        const friendBtn = target?.closest<HTMLElement>('[data-role="avatar-btn"]');
        if (friendBtn) {
            const img = friendBtn.querySelector("img");
            this.addFriend({ id: Number(friendBtn.dataset.id), username: friendBtn.dataset.username ?? "", avatar_url: img?.getAttribute("src") ?? "" });
            return;
        }
        const suggestion = target?.closest<HTMLElement>(".safety-contact-suggestion");
        const friend = this.friends.find((f) => String(f.id) === suggestion?.dataset.id);
        const input = this.part("input", HTMLInputElement);
        if (friend && input) {
            this.addFriend(friend);
            input.value = "";
            this.hideSuggestions();
            input.focus();
        }
    }

    private onKeydown(event: KeyboardEvent, input: HTMLInputElement): void {
        if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            this.commit(input);
        } else if (event.key === " ") {
            // A space after a whole email address moves on to the next; within a name it is just a space.
            const typed = input.value.trim();
            if (EMAIL_RE.test(typed)) {
                event.preventDefault();
                this.addEmail(typed);
                input.value = "";
                this.hideSuggestions();
            }
        } else if (event.key === "Escape") {
            this.hideSuggestions();
        }
    }

    private addedIds(): number[] {
        return Array.from(this.chips.querySelectorAll<HTMLElement>('[data-type="profile"]'), (c) => Number(c.dataset.id));
    }

    private addedEmails(): string[] {
        return Array.from(this.chips.querySelectorAll<HTMLElement>('[data-type="email"]'), (c) => (c.dataset.email ?? "").toLowerCase());
    }

    private changed(): void {
        this.root.dispatchEvent(new CustomEvent("contactschange", { bubbles: true }));
    }

    private syncAvatarButtons(): void {
        const ids = this.addedIds();
        this.root.querySelectorAll<HTMLElement>('[data-role="avatar-btn"]').forEach((btn) => {
            btn.hidden = ids.includes(Number(btn.dataset.id));
        });
    }

    addFriend(friend: ContactFriend): void {
        if (this.addedIds().includes(friend.id)) return;
        const span = chip("profile", avatar("safety-contact-chip-avatar", friend.avatar_url, friend.username.charAt(0).toUpperCase()), friend.username, "contact_profile_ids", String(friend.id));
        span.dataset.id = String(friend.id);
        this.chips.append(span);
        this.syncAvatarButtons();
        this.changed();
    }

    addEmail(raw: string): void {
        const email = raw.trim().toLowerCase();
        if (!email || this.addedEmails().includes(email)) return;
        const face = document.createElement("span");
        face.className = "safety-contact-chip-avatar safety-contact-chip-avatar--placeholder";
        face.append(icon("mail"));
        const span = chip("email", face, email, "contact_emails", email);
        span.dataset.email = email;
        this.chips.append(span);
        this.changed();
    }

    /** Add what was typed if it names a friend exactly or is an email address. */
    private commit(input: HTMLInputElement): boolean {
        const value = input.value.trim().replace(/,$/, "");
        if (!value) return false;
        const exact = this.friends.find((f) => f.username.toLowerCase() === value.toLowerCase());
        if (exact && !this.addedIds().includes(exact.id)) this.addFriend(exact);
        else if (EMAIL_RE.test(value)) this.addEmail(value);
        else return false;
        input.value = "";
        this.hideSuggestions();
        return true;
    }

    private hideSuggestions(): void {
        const box = this.part("suggestions", HTMLElement);
        if (box) box.hidden = true;
    }

    private renderSuggestions(input: HTMLInputElement): void {
        const box = this.part("suggestions", HTMLElement);
        if (!box) return;
        box.replaceChildren();
        const q = input.value.trim().toLowerCase();
        const added = this.addedIds();
        const matches = q
            ? this.friends.filter((f) => !added.includes(f.id) && (f.username.toLowerCase().includes(q) || !!f.full_name?.toLowerCase().includes(q))).slice(0, MAX_SUGGESTIONS)
            : [];
        for (const f of matches) {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "safety-contact-suggestion";
            item.dataset.id = String(f.id);
            const label = document.createElement("span");
            label.className = "safety-contact-suggestion-label";
            label.textContent = f.full_name ? `${f.username} (${f.full_name})` : f.username;
            item.append(avatar("safety-contact-suggestion-avatar", f.avatar_url, f.username.charAt(0).toUpperCase()), label);
            // Keep focus in the input, so its blur doesn't commit what is half typed.
            item.addEventListener("mousedown", (event) => event.preventDefault());
            box.append(item);
        }
        box.hidden = matches.length === 0;
    }
}

/** Wire every contact picker under *scope* that isn't wired yet. */
export function initContactPickers(scope: ParentNode = document): void {
    scope.querySelectorAll<HTMLElement>('.safety-contact-picker[data-role="contact-picker"]').forEach((root) => {
        if (root.dataset.contactPickerInit) return;
        const chips = root.querySelector<HTMLElement>('[data-role="chips"]');
        if (!chips) return;
        root.dataset.contactPickerInit = "1";
        new ContactPicker(root, chips).install();
    });
}
