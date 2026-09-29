/**
 * The profile page (pages/profile/index.html): the external-link warning, the own-profile edit-in-place fields,
 * the photo strip's lightbox, and the private-note edit toggles.
 */

import { escHtml } from "../shared/escape-html";

interface FieldResponse {
    error?: string;
    pending?: boolean;
    message?: string;
}

interface MetaFieldConfig {
    selector: string;
    dataKey: string;
    field: string;
    inputType: string;
    successMessage: string;
    errorMessage: string;
    renderText: (value: string) => string;
}

interface IdentityFieldConfig {
    selector: string;
    textSelector?: string;
    dataKey: string;
    field: string;
    inputType?: string;
    maxLength?: number;
    successMessage: string;
    renderText: (value: string) => string;
    afterSave?: (value: string) => void;
}

function metaText(text: string): string {
    return '<span class="profile-meta-text">' + escHtml(text) + "</span>";
}

function onActivate(el: HTMLElement, start: () => void): void {
    el.addEventListener("click", start);
    el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            start();
        }
    });
}

function bindExternalLinkWarning(): void {
    const overlay = document.getElementById("ext-warn-overlay");
    const urlEl = document.getElementById("ext-warn-url");
    const continueEl = document.getElementById("ext-warn-continue");
    const cancelBtn = document.getElementById("ext-warn-cancel");
    if (!overlay || !urlEl || !(continueEl instanceof HTMLAnchorElement) || !cancelBtn) return;

    const openDialog = (url: string): void => {
        urlEl.textContent = url;
        continueEl.href = url;
        overlay.style.display = "flex";
        overlay.setAttribute("aria-hidden", "false");
        cancelBtn.focus();
    };
    const closeDialog = (): void => {
        overlay.style.display = "none";
        overlay.setAttribute("aria-hidden", "true");
        continueEl.href = "#";
    };

    document.querySelectorAll<HTMLElement>("[data-external-url]").forEach((el) => {
        el.addEventListener("click", (e) => {
            e.preventDefault();
            openDialog(el.dataset.externalUrl ?? "");
        });
    });
    cancelBtn.addEventListener("click", closeDialog);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeDialog();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay.style.display !== "none") closeDialog();
    });
}

function bindViewAsMenu(): void {
    const viewAsMenu = document.querySelector<HTMLDetailsElement>(".profile-viewas");
    if (!viewAsMenu) return;
    document.addEventListener("click", (e) => {
        if (viewAsMenu.open && e.target instanceof Node && !viewAsMenu.contains(e.target)) viewAsMenu.open = false;
    });
}

/** Swap a private note between its text and its edit box. */
function toggleNoteEdit(noteId: string): void {
    const body = document.getElementById("pan-note-body-" + noteId);
    const edit = document.getElementById("pan-note-edit-" + noteId);
    if (!body || !edit) return;
    const isEditing = !edit.hidden;
    body.hidden = !isEditing;
    edit.hidden = isEditing;
    const textarea = edit.querySelector("textarea");
    if (!isEditing && textarea) {
        textarea.focus();
        // It read a scrollHeight of 0 while hidden, so it is sized now.
        textarea.style.height = "auto";
        textarea.style.height = textarea.scrollHeight + "px";
    }
}

/** Open the lightbox on the photo strip, over every tile that has a file to show. */
function openStripPhoto(tile: HTMLElement, isOwnProfile: boolean): void {
    const tiles = Array.from(document.querySelectorAll<HTMLElement>(".profile-photo-strip-item")).filter((el) => el.dataset.photoUrl || el.dataset.url);
    const list = tiles.map((el) => ({
        url: el.dataset.photoUrl || el.dataset.url || "",
        caption: el.dataset.photoCaption || "",
        takenAt: el.dataset.photoTakenAt || "",
        imageId: el.dataset.photoId ? parseInt(el.dataset.photoId, 10) : null,
        // The strip can show another profile's photos; own-photo lightbox actions must not be offered on them.
        isMine: isOwnProfile,
    }));
    window.galleryOpenLightboxItem?.(list, Math.max(0, tiles.indexOf(tile)));
}

function bindDelegatedActions(root: HTMLElement): void {
    const isOwnProfile = root.dataset.ownProfile === "true";
    // Notes are re-rendered by htmx, so their buttons are handled at the document.
    document.addEventListener("click", (e) => {
        if (!(e.target instanceof Element)) return;
        const noteToggle = e.target.closest<HTMLElement>("[data-pan-toggle-edit]");
        if (noteToggle) {
            toggleNoteEdit(noteToggle.dataset.panToggleEdit ?? "");
            return;
        }
        const stripTile = e.target.closest<HTMLElement>(".profile-photo-strip-item");
        if (stripTile) openStripPhoto(stripTile, isOwnProfile);
    });
}

function bindOwnProfileEditing(updateUrl: string): void {
    function post(field: string, value: string): Promise<Response> {
        const body = new URLSearchParams();
        body.set("field", field);
        body.set("value", value);
        return fetch(updateUrl, { method: "POST", headers: { "X-CSRFToken": csrftoken }, body });
    }

    // The identity fields' endpoint returns a specific reason as JSON, sometimes with a 200.
    function postFieldWithErrors(field: string, value: string): Promise<FieldResponse> {
        return post(field, value).then((r) =>
            r
                .json()
                .catch(() => ({}))
                .then((data: FieldResponse) => {
                    if (!r.ok || data.error) throw new Error(data.error || "Something went wrong.");
                    return data;
                }),
        );
    }

    const bioEl = document.querySelector<HTMLElement>(".profile-bio-full--editable");
    if (bioEl) {
        const placeholder = "Add a bio...";
        const bio = bioEl;
        onActivate(bio, () => {
            if (bio.querySelector("textarea")) return;
            const rawBio = bio.dataset.rawBio || "";
            const textarea = document.createElement("textarea");
            textarea.className = "profile-bio-input";
            textarea.value = rawBio;
            textarea.placeholder = placeholder;
            textarea.maxLength = 50000;
            textarea.rows = 3;

            window.urbanlensSizeEditInPlaceInput(bio, textarea);
            const displayText = bio.textContent;
            bio.textContent = "";
            bio.appendChild(textarea);
            textarea.style.height = "auto";
            textarea.style.height = textarea.scrollHeight + "px";
            textarea.focus();

            let done = false;
            const finish = (save: boolean): void => {
                if (done) return;
                done = true;
                const newBio = textarea.value.trim();
                if (!save || newBio === rawBio.trim()) {
                    bio.textContent = displayText;
                    return;
                }
                post("bio", newBio)
                    .then((r) => {
                        if (!r.ok) throw new Error();
                    })
                    .then(() => {
                        bio.dataset.rawBio = newBio;
                        bio.textContent = newBio || placeholder;
                        window.toastr?.success("Bio updated.");
                    })
                    .catch(() => {
                        bio.textContent = displayText;
                        window.toastr?.error("Failed to update bio.");
                    });
            };
            textarea.addEventListener("blur", () => finish(true));
            textarea.addEventListener("keydown", (e) => {
                e.stopPropagation();
                if (e.key === "Escape") {
                    e.preventDefault();
                    finish(false);
                }
            });
        });
    }

    function wireMetaField(config: MetaFieldConfig): void {
        const el = document.querySelector<HTMLElement>(config.selector);
        if (!el) return;
        onActivate(el, () => {
            if (el.querySelector("input")) return;
            // Re-queried each time: a save replaces this node's outerHTML.
            const textEl = el.querySelector(".profile-meta-text");
            if (!textEl) return;
            const rawValue = el.dataset[config.dataKey] || "";
            const input = document.createElement("input");
            input.className = "profile-meta-input";
            input.type = config.inputType;
            input.value = rawValue;

            window.urbanlensSizeEditInPlaceInput(textEl, input);
            const displayHTML = textEl.outerHTML;
            textEl.replaceWith(input);
            input.focus();

            let done = false;
            const finish = (save: boolean): void => {
                if (done) return;
                done = true;
                const newValue = input.value.trim();
                if (!save || newValue === rawValue.trim()) {
                    input.outerHTML = displayHTML;
                    return;
                }
                post(config.field, newValue)
                    .then((r) => {
                        if (!r.ok) throw new Error();
                    })
                    .then(() => {
                        el.dataset[config.dataKey] = newValue;
                        input.outerHTML = config.renderText(newValue);
                        window.toastr?.success(config.successMessage);
                    })
                    .catch(() => {
                        input.outerHTML = displayHTML;
                        window.toastr?.error(config.errorMessage);
                    });
            };
            input.addEventListener("blur", () => finish(true));
            input.addEventListener("keydown", (e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                    e.preventDefault();
                    input.blur();
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    finish(false);
                }
            });
        });
    }

    wireMetaField({
        selector: ".profile-area--editable",
        dataKey: "rawArea",
        field: "area",
        inputType: "text",
        successMessage: "Area updated.",
        errorMessage: "Failed to update area.",
        renderText: (value) => metaText(value || "Add your area..."),
    });

    wireMetaField({
        selector: ".profile-started-exploring--editable",
        dataKey: "rawStartedExploring",
        field: "started_exploring",
        inputType: "date",
        successMessage: "Updated.",
        errorMessage: "Failed to update.",
        renderText: (value) => metaText(value ? "Exploring since " + value.slice(0, 4) : "Add when you started exploring..."),
    });

    const simpleTextFields: Array<[string, string, string, string, string]> = [
        [".profile-phone-editable", "rawPhone", "phone_number", "Add phone number...", "Phone"],
        [".profile-whatsapp-editable", "rawWhatsapp", "whatsapp_number", "Add WhatsApp...", "WhatsApp"],
        [".profile-signal-editable", "rawSignal", "signal_username", "Add Signal...", "Signal"],
        [".profile-telegram-editable", "rawTelegram", "telegram_username", "Add Telegram...", "Telegram"],
        [".profile-discord-editable", "rawDiscord", "discord_username", "Add Discord...", "Discord"],
        [".profile-matrix-editable", "rawMatrix", "matrix_handle", "Add Matrix...", "Matrix"],
    ];
    simpleTextFields.forEach(([selector, dataKey, field, placeholder, label]) =>
        wireMetaField({
            selector,
            dataKey,
            field,
            inputType: "text",
            successMessage: label + " updated.",
            errorMessage: "Failed to update " + label.toLowerCase() + ".",
            renderText: (value) => metaText(value || placeholder),
        }),
    );

    wireMetaField({
        selector: ".profile-birth-date-editable",
        dataKey: "rawBirthDate",
        field: "birth_date",
        inputType: "date",
        successMessage: "Birthday updated.",
        errorMessage: "Failed to update birthday.",
        renderText: (value) => {
            if (!value) return metaText("Add your birthday...");
            const formatted = new Date(value + "T00:00:00").toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
            return metaText("Birthday: " + formatted);
        },
    });

    function wireIdentityField(config: IdentityFieldConfig): void {
        const el = document.querySelector<HTMLElement>(config.selector);
        if (!el) return;
        onActivate(el, () => {
            if (el.querySelector("input")) return;
            const target = (config.textSelector ? el.querySelector(config.textSelector) : null) ?? el;
            const rawValue = el.dataset[config.dataKey] || "";
            const input = document.createElement("input");
            input.type = config.inputType || "text";
            input.className = "profile-meta-input";
            input.value = rawValue;
            if (config.maxLength) input.maxLength = config.maxLength;

            window.urbanlensSizeEditInPlaceInput(target, input);
            const displayHTML = target.outerHTML;
            target.replaceWith(input);
            input.focus();
            input.select();

            let done = false;
            const finish = (save: boolean): void => {
                if (done) return;
                done = true;
                const newValue = input.value.trim();
                if (!save || newValue === rawValue.trim() || !newValue) {
                    input.outerHTML = displayHTML;
                    return;
                }
                postFieldWithErrors(config.field, newValue)
                    .then((data) => {
                        if (data.pending) {
                            input.outerHTML = displayHTML;
                            window.toastr?.info(data.message ?? "");
                            return;
                        }
                        el.dataset[config.dataKey] = newValue;
                        input.outerHTML = config.renderText(newValue);
                        window.toastr?.success(config.successMessage);
                        config.afterSave?.(newValue);
                    })
                    .catch((err: unknown) => {
                        input.outerHTML = displayHTML;
                        window.toastr?.error(err instanceof Error && err.message ? err.message : "Something went wrong.");
                    });
            };
            input.addEventListener("blur", () => finish(true));
            input.addEventListener("keydown", (e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                    e.preventDefault();
                    input.blur();
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    finish(false);
                }
            });
        });
    }

    wireIdentityField({
        selector: ".profile-email-editable",
        textSelector: ".profile-meta-text",
        dataKey: "rawEmail",
        field: "email",
        inputType: "email",
        maxLength: 254,
        successMessage: "Email updated.",
        renderText: (value) => metaText(value),
    });

    const usernameField: IdentityFieldConfig = {
        selector: ".profile-username-editable",
        dataKey: "rawUsername",
        field: "username",
        maxLength: 30,
        successMessage: "Username updated.",
        renderText: (value) =>
            '<span class="profile-username-editable" tabindex="0" role="button" data-raw-username="' + escHtml(value) + '" title="Click to change your username">' + escHtml(value) + "</span>",
        afterSave: (value) => {
            // The username is the name field's fallback display when first and last are empty.
            const nameEl = document.querySelector<HTMLElement>(".profile-name-editable");
            if (nameEl) {
                nameEl.dataset.username = value;
                if (!(nameEl.dataset.rawFirst || nameEl.dataset.rawLast)) nameEl.textContent = value;
            }
            // The element was replaced wholesale, taking its listeners with it.
            wireIdentityField(usernameField);
        },
    };
    wireIdentityField(usernameField);

    // Two backend fields behind one click; focus leaving both saves whichever changed, Escape cancels both.
    const nameEl = document.querySelector<HTMLElement>(".profile-name-editable");
    if (nameEl) {
        const el = nameEl;
        onActivate(el, () => {
            if (el.querySelector("input")) return;
            const rawFirst = el.dataset.rawFirst || "";
            const rawLast = el.dataset.rawLast || "";
            const displayText = el.textContent;

            const wrap = document.createElement("span");
            wrap.className = "profile-name-edit-wrap";
            const makeInput = (placeholder: string, value: string): HTMLInputElement => {
                const input = document.createElement("input");
                input.type = "text";
                input.className = "profile-meta-input";
                input.placeholder = placeholder;
                input.value = value;
                input.maxLength = 150;
                wrap.appendChild(input);
                return input;
            };
            const firstInput = makeInput("First name", rawFirst);
            const lastInput = makeInput("Last name", rawLast);

            window.urbanlensSizeEditInPlaceInput(el, wrap);
            el.textContent = "";
            el.appendChild(wrap);
            firstInput.focus();
            firstInput.select();

            let done = false;
            const finish = (save: boolean): void => {
                if (done) return;
                done = true;
                const newFirst = firstInput.value.trim();
                const newLast = lastInput.value.trim();
                if (!save || (newFirst === rawFirst.trim() && newLast === rawLast.trim())) {
                    el.textContent = displayText;
                    return;
                }
                const saves: Promise<FieldResponse>[] = [];
                if (newFirst !== rawFirst.trim()) saves.push(postFieldWithErrors("first_name", newFirst));
                if (newLast !== rawLast.trim()) saves.push(postFieldWithErrors("last_name", newLast));
                Promise.all(saves)
                    .then(() => {
                        el.dataset.rawFirst = newFirst;
                        el.dataset.rawLast = newLast;
                        el.textContent = (newFirst + " " + newLast).trim() || el.dataset.username || "";
                        window.toastr?.success("Name updated.");
                    })
                    .catch((err: unknown) => {
                        el.textContent = displayText;
                        window.toastr?.error(err instanceof Error && err.message ? err.message : "Failed to update name.");
                    });
            };

            // Tabbing between the two inputs is not leaving.
            wrap.addEventListener("focusout", (e) => {
                if (e.relatedTarget instanceof Node && wrap.contains(e.relatedTarget)) return;
                finish(true);
            });
            wrap.addEventListener("keydown", (e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                    e.preventDefault();
                    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    finish(false);
                }
            });
        });
    }
}

const root = document.querySelector<HTMLElement>(".profile-page");
if (root) {
    bindExternalLinkWarning();
    bindViewAsMenu();
    bindDelegatedActions(root);
    if (root.dataset.fieldUpdateUrl) bindOwnProfileEditing(root.dataset.fieldUpdateUrl);
}
