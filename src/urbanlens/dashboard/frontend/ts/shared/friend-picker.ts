/**
 * The games' friend invite picker: a server-rendered checkbox list (``GameFriendPickerView``) that htmx loads.
 *
 * The page markup comes from ``partials/games/_friend_picker.html``; the mid-game "invite more" dialog builds the same
 * element here. The ticked checkboxes are the selection, so nothing has to be kept in step with them.
 */

const PICKER = "[data-friend-picker]";
const CHOICE = 'input[name="invite_profile_ids"]';
export const FRIEND_PICKER_RELOAD = "ul:friend-picker-reload";
const LOADING_HTML = '<p class="ul-game-friend-picker__hint">Loading friends&hellip;</p>';
const FAILED_HTML =
    '<p class="ul-game-friend-picker__hint">Couldn\'t load your friends list. <button type="button" class="btn btn--link" data-friend-picker-retry>Retry</button></p>';
const FAILURE_EVENTS = ["htmx:responseError", "htmx:sendError", "htmx:timeout"] as const;

/** Profile ids ticked in *root*. */
export function selectedFriendIds(root: ParentNode): number[] {
    return Array.from(root.querySelectorAll<HTMLInputElement>(`${CHOICE}:checked`), (input) => Number(input.value)).filter(
        (id) => Number.isInteger(id) && id > 0,
    );
}

/** Untick every friend in *root*. */
export function clearFriendSelection(root: ParentNode): void {
    root.querySelectorAll<HTMLInputElement>(CHOICE).forEach((input) => {
        input.checked = false;
    });
}

/** Whether *root* offers at least one friend. */
export function hasFriendChoices(root: ParentNode): boolean {
    return root.querySelector(CHOICE) !== null;
}

function pickerFor(event: Event): HTMLElement | null {
    const target = event.target;
    return target instanceof HTMLElement && target.matches(PICKER) ? target : null;
}

function onLoadFailed(event: Event): void {
    const picker = pickerFor(event);
    if (picker) picker.innerHTML = FAILED_HTML;
}

function onRetry(event: Event): void {
    const button = event.target instanceof Element ? event.target.closest("[data-friend-picker-retry]") : null;
    const picker = button?.closest<HTMLElement>(PICKER);
    if (!picker) return;
    picker.innerHTML = LOADING_HTML;
    window.htmx?.trigger(picker, FRIEND_PICKER_RELOAD);
}

/**
 * Replace a picker's loading line with a retry prompt when its request fails (base.html's htmx handler toasts it).
 *
 * Named listeners, so a second call from the same bundle adds nothing.
 */
export function installFriendPicker(): void {
    FAILURE_EVENTS.forEach((name) => document.addEventListener(name, onLoadFailed));
    document.addEventListener("click", onRetry);
}

/**
 * Turn *list* into a picker loading from *url*, leaving out *exclude*.
 */
export function mountFriendPicker(list: HTMLElement, url: string, exclude: Iterable<number> = []): void {
    const ids = [...new Set(exclude)].filter((id) => Number.isInteger(id) && id > 0);
    const separator = url.includes("?") ? "&" : "?";
    list.classList.add("ul-game-friend-picker");
    list.dataset.friendPicker = "";
    list.setAttribute("hx-get", ids.length ? `${url}${separator}exclude=${ids.join(",")}` : url);
    list.setAttribute("hx-trigger", `load, ${FRIEND_PICKER_RELOAD}`);
    list.setAttribute("hx-swap", "innerHTML");
    list.innerHTML = LOADING_HTML;
    window.htmx?.process(list);
}

export interface PickFriendsOptions {
    /** The picker endpoint (``games.friends``). */
    url: string;
    /** Profiles already in the game. */
    exclude: Iterable<number>;
    /** Attach the dialog; a game in true fullscreen must mount it inside its shell to have it painted. */
    mount: (dialog: HTMLDialogElement) => void;
}

/**
 * Ask which friends to invite, in a modal. Resolves with the chosen profile ids, or none if cancelled.
 */
export function pickFriendsToInvite(options: PickFriendsOptions): Promise<number[]> {
    return new Promise((resolve) => {
        const dialog = document.createElement("dialog");
        dialog.className = "ul-dialog ul-game-dialog ul-game-invite-dialog";

        const header = document.createElement("div");
        header.className = "dialog-header";
        const heading = document.createElement("h3");
        heading.textContent = "Invite more players";
        header.appendChild(heading);

        const body = document.createElement("div");
        body.className = "ul-dialog-body";
        const list = document.createElement("div");
        body.appendChild(list);

        const actions = document.createElement("div");
        actions.className = "dialog-footer";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn--ghost";
        cancelBtn.textContent = "Cancel";
        const inviteBtn = document.createElement("button");
        inviteBtn.type = "button";
        inviteBtn.className = "btn btn--primary";
        inviteBtn.textContent = "Invite";
        inviteBtn.disabled = true;
        actions.append(cancelBtn, inviteBtn);

        dialog.append(header, body, actions);
        options.mount(dialog);
        mountFriendPicker(list, options.url, options.exclude);
        list.addEventListener("htmx:afterSwap", () => {
            inviteBtn.disabled = !hasFriendChoices(list);
        });

        let settled = false;
        const finish = (result: number[]): void => {
            if (settled) return;
            settled = true;
            if (dialog.open) dialog.close();
            dialog.remove();
            resolve(result);
        };
        cancelBtn.addEventListener("click", () => finish([]));
        inviteBtn.addEventListener("click", () => finish(selectedFriendIds(list)));
        dialog.addEventListener("cancel", () => finish([]));

        dialog.showModal();
    });
}
