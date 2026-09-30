/**
 * The Messages page (pages/messages/index.html): the conversation list, the open thread, its composer and the
 * live socket. The thread pane is swapped by htmx on every conversation switch, so everything inside it is
 * reached through delegation or looked up when needed.
 *
 * Encryption goes through the page's classic ``e2ee.js`` bundle (``window.UrbanLensE2EE``), which holds the
 * unlocked keys; importing e2ee-client here would give this bundle a second, locked copy.
 */

import { buildBubble, closeEmojiPopovers, installReactionPopovers, needsServerRender, renderReactionBar, tombstone } from "../shared/dm-bubbles";
import { PendingAttachments, shareFromPick, type StagedShare } from "../shared/dm-attachments";
import { parseDmFrame, type DmFrame, type DmMessage, type DmReaction } from "../shared/dm-frames";
import { DmLightbox } from "../shared/dm-lightbox";
import { MentionMenu, type MentionKind } from "../shared/dm-mention-menu";
import { byId } from "../shared/dom";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { e2eeUrlsFromDataset, SLUG_TOKEN, UUID_TOKEN } from "../shared/e2ee-urls";
import { fetchJson, fetchText, HttpError } from "../shared/fetch-json";
import { htmxDetail } from "../shared/htmx-events";
import { openLiveSocket, type LiveSocketHandle } from "../shared/live-socket";
import { observeProcessingTiles, settleProcessingThumb } from "../shared/photo-processing";
import { startPoller } from "../shared/poller";
import type { CommentMapComposerOptions } from "../types/globals";

type E2EE = Window["UrbanLensE2EE"];

/** Matches the composer textarea's CSS max-height. */
const COMPOSER_MAX_HEIGHT = 160;

/** For inserting into the message text; reactions use their own set. */
const INSERT_EMOJIS = ["😀", "😂", "😍", "🥰", "😎", "🤔", "😢", "😮", "😡", "👍", "👎", "🙏", "👋", "🎉", "❤️", "🔥", "⭐", "✅", "❌", "📍", "📷", "🗺️", "🚶", "🏕️", "🌇", "🌙"];

/** A tab reconnecting this many times in a row is told its messages may be late. */
const SLOW_RECONNECT_WARNING_AFTER = 5;

const SIDEBAR_COLLAPSED_KEY = "ul_dm_sidebar_collapsed";

interface PageUrls {
    /** ``messages.react`` for slug token and message 0. */
    react: string;
    /** ``messages.share.pin`` for the slug token; the kind replaces ``pin``. */
    share: string;
    groupSharePin: string;
    groupMembers: string;
    groupCreate: string;
    uploadImage: string;
    mapPicker: string;
}

interface OutgoingContent {
    body: string;
    ciphertext?: string;
    nonce?: string;
    keyVersion?: number;
}

function elementOf(event: Event): Element | null {
    return event.target instanceof Element ? event.target : null;
}

function composerInput(): HTMLTextAreaElement | null {
    return byId("dm-composer-input", HTMLTextAreaElement);
}

function activeThread(): HTMLElement | null {
    return document.getElementById("dm-thread");
}

function activeSlug(): string {
    return activeThread()?.dataset.partnerSlug ?? "";
}

function activeGroup(): string {
    return activeThread()?.dataset.groupUuid ?? "";
}

/** The server's own words for a refusal, or *fallback* when all it sent was a status or a page. */
function failureMessage(err: unknown, fallback: string): string {
    return err instanceof HttpError && !/^HTTP \d+$/.test(err.message) ? err.message : fallback;
}

function autoExpand(el: HTMLTextAreaElement | null): void {
    if (!el) return;
    el.style.height = "auto";
    // An empty textarea can report a scrollHeight a pixel or two over its clientHeight; scroll only past the cap.
    const overflowing = el.scrollHeight > COMPOSER_MAX_HEIGHT;
    el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_HEIGHT)}px`;
    el.style.overflowY = overflowing ? "auto" : "hidden";
}

function resetComposerInput(input: HTMLTextAreaElement | null): void {
    if (!input) return;
    input.value = "";
    autoExpand(input);
}

function scrollMessages(): void {
    const list = document.getElementById("dm-messages");
    if (list) list.scrollTop = list.scrollHeight;
}

function refreshList(): void {
    window.htmx?.trigger(document.body, "dmListRefresh");
    window.htmx?.trigger(document.body, "msgCountRefresh");
}

function closeBubbleMenus(except?: Element | null): void {
    document.querySelectorAll<HTMLElement>(".dm-bubble-menu").forEach((m) => {
        if (m !== except) m.hidden = true;
    });
}

/** Swap server-rendered thread markup into the pane, as a send's response does. */
function replacePane(html: string): void {
    const pane = document.getElementById("dm-thread-pane");
    if (!pane) return;
    pane.innerHTML = html;
    window.htmx?.process(pane);
    window._initThumbs?.();
}

/**
 * The sidebar's active row is server-rendered, but a plain conversation click swaps only the thread pane, so the
 * previous row would stay highlighted until the list next re-renders.
 */
function syncActiveConversationHighlight(): void {
    document.querySelectorAll(".dm-conv-item--active").forEach((el) => el.classList.remove("dm-conv-item--active"));
    const group = activeGroup();
    const slug = activeSlug();
    const match = Array.from(document.querySelectorAll<HTMLElement>(".dm-conv-item")).find((el) => (group ? el.dataset.groupUuid === group : !!slug && el.dataset.partnerSlug === slug));
    match?.classList.add("dm-conv-item--active");
}

/** A panel toggled by a button, closed by Escape or a click outside both. */
function installTogglePanel(button: HTMLElement | null, panel: HTMLElement | null, focusId: string, outsideCloses: boolean): (() => void) | null {
    if (!button || !panel) return null;
    const close = (): void => {
        if (panel.hidden) return;
        panel.hidden = true;
        button.setAttribute("aria-expanded", "false");
    };
    button.addEventListener("click", (e) => {
        // Kept from the document handlers, so this click does not count as outside another panel.
        e.stopPropagation();
        const open = !panel.hidden;
        panel.hidden = open;
        button.setAttribute("aria-expanded", String(!open));
        if (!open) document.getElementById(focusId)?.focus();
    });
    if (outsideCloses) {
        document.addEventListener("click", (e) => {
            if (panel.hidden || !(e.target instanceof Node)) return;
            if (!panel.contains(e.target) && !button.contains(e.target)) close();
        });
    }
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") close();
    });
    return close;
}

class MessagesPage {
    private readonly mySlug: string;
    private readonly urls: PageUrls;
    private readonly e2ee: E2EE | null;
    private readonly pending = new PendingAttachments();
    private readonly lightbox = new DmLightbox();
    private readonly mention: MentionMenu;
    private socket: LiveSocketHandle | null = null;
    private reconnects = 0;
    private typingHideTimer: ReturnType<typeof setTimeout> | undefined;
    private lastTypingSentAt = 0;
    private olderScrollHeight: number | null = null;
    private closeMessagesSearch: () => void = () => undefined;

    constructor(private readonly page: HTMLElement) {
        const d = page.dataset;
        this.mySlug = d.mySlug ?? "";
        this.urls = {
            react: d.reactUrl ?? "",
            share: d.shareUrl ?? "",
            groupSharePin: d.groupSharePinUrl ?? "",
            groupMembers: d.groupMembersUrl ?? "",
            groupCreate: d.groupCreateUrl ?? "",
            uploadImage: d.uploadImageUrl ?? "",
            mapPicker: d.mapPickerUrl ?? "",
        };
        this.e2ee = window.UrbanLensE2EE ?? null;
        this.e2ee?.init({ selfSlug: this.mySlug, loginIdentifier: d.loginIdentifier ?? "", urls: e2eeUrlsFromDataset(d) });
        this.mention = new MentionMenu(composerInput, (kind) => this.onMentionPicked(kind), autoExpand);
    }

    install(): void {
        this.lightbox.install();
        installReactionPopovers();
        const pane = document.getElementById("dm-thread-pane");
        if (pane) observeProcessingTiles(pane, (el, item) => settleProcessingThumb(el, item, "dm-bubble__image", "full"));
        document.addEventListener("htmx:configRequest", (e) => {
            const { elt, parameters } = htmxDetail(e);
            if (!elt?.classList.contains("dm-sidebar__list") || !parameters) return;
            parameters.active = activeSlug();
            parameters.active_group = activeGroup();
        });

        this.installComposerKeys();
        this.installThreadClicks();
        this.connect();
        document.addEventListener("submit", (e) => this.onSubmit(e));
        this.installGroupControls();
        this.installAttachments();
        this.installMentionAndEmoji();
        this.installSidebar();
        this.installThreadSearch();
        this.installSwaps();
        this.installThreadActions();

        window.addEventListener("hashchange", () => this.highlightMessageFromHash());
        this.highlightMessageFromHash();
        // A back/forward-cache restore shows the DOM as it was left: stale share cards, consent prompts, read state.
        window.addEventListener("pageshow", (ev) => {
            if (!ev.persisted) return;
            refreshList();
            if (activeThread()) void this.refreshActiveThread();
        });

        this.refreshEncryption();
        this.decryptSidebarPreviews();
        autoExpand(composerInput());
        scrollMessages();
    }

    // -- Encryption ------------------------------------------------------------------------------

    private threadEncrypts(): boolean {
        const d = activeThread()?.dataset;
        return d?.partnerEncrypts === "1" || d?.groupEncrypts === "1";
    }

    /** Decrypt what is pending in the open thread, and reflect this device's lock state in the banner and composer. */
    private refreshEncryption(): void {
        const e2ee = this.e2ee;
        const thread = activeThread();
        if (!e2ee || !thread) return;
        e2ee.decryptDom(thread, thread.dataset.partnerSlug).catch((err: unknown) => console.error("E2EE decrypt pass failed", err));
        if (!this.threadEncrypts()) return;
        void e2ee.getUnlockState().then((state) => {
            const locked = state === "locked";
            const banner = document.getElementById("dm-locked-banner");
            if (banner) banner.hidden = !locked;
            const input = composerInput();
            if (input) {
                input.disabled = locked;
                if (locked) input.placeholder = "Unlock this device to send encrypted messages";
            }
            const send = document.querySelector<HTMLButtonElement>("#dm-composer .dm-composer__send");
            if (send) send.disabled = locked;
        });
    }

    /**
     * The fields to send for *body*: ciphertext when the conversation encrypts, plaintext when it cannot (nobody
     * to encrypt to, or this device is locked). Null when encryption was expected and failed - sending the
     * plaintext then would put a readable message in a thread both people believe is encrypted.
     */
    private async outgoingContent(partner: string, body: string): Promise<OutgoingContent | null> {
        const group = activeGroup();
        const e2ee = this.e2ee;
        if (!e2ee || !body || !this.threadEncrypts()) return { body };
        try {
            const result = group ? await e2ee.encryptForGroup(group, body) : await e2ee.encryptForPartner(partner, body);
            if (result.status === "encrypted") {
                const enc = result.payload;
                return { body: "", ciphertext: enc.ciphertext, nonce: enc.nonce, keyVersion: enc.key_version };
            }
            if (result.status === "unencryptable") return { body };
            return this.encryptionFailed(result.reason);
        } catch (err) {
            return this.encryptionFailed(err);
        }
    }

    private encryptionFailed(reason: unknown): null {
        console.error("E2EE encrypt failed; message not sent", reason);
        toast.error("Could not encrypt this message, so it was not sent. Check your connection and try again.");
        return null;
    }

    private decryptSidebarPreviews(): void {
        const e2ee = this.e2ee;
        if (!e2ee) return;
        document.querySelectorAll<HTMLElement>("#dm-conversation-list [data-e2ee-preview]").forEach((node) => {
            const { partner, group, ct, nonce } = node.dataset;
            const kv = Number.parseInt(node.dataset.kv ?? "0", 10);
            if ((!partner && !group) || !ct || !nonce || !kv) return;
            // Keep the sender prefix before the lock placeholder: "You: " on 1:1 rows, "<name>: " on group rows.
            const text = node.textContent ?? "";
            const lockIndex = text.indexOf("🔒");
            const prefix = lockIndex > 0 ? text.slice(0, lockIndex) : "";
            node.removeAttribute("data-e2ee-preview");
            const decrypting = group ? e2ee.decryptFromGroup(group, ct, nonce, kv) : e2ee.decryptFromPartner(partner ?? "", ct, nonce, kv);
            decrypting
                .then((plaintext) => {
                    if (plaintext !== null) node.textContent = prefix + (plaintext.length > 70 ? `${plaintext.slice(0, 69)}…` : plaintext);
                })
                .catch(() => undefined);
        });
    }

    // -- Composer --------------------------------------------------------------------------------

    private installComposerKeys(): void {
        document.addEventListener("input", (e) => {
            if (e.target !== composerInput()) return;
            autoExpand(composerInput());
            this.maybeSendTyping();
            this.mention.track();
            if (this.mention.isOpen) closeEmojiPopovers();
        });
        document.addEventListener("keydown", (e) => {
            const input = composerInput();
            if (!input || e.target !== input) return;
            if (this.mention.handleKey(e)) return;
            if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
            e.preventDefault();
            input.form?.requestSubmit();
        });
    }

    private cancelReply(): void {
        const preview = document.getElementById("dm-reply-preview");
        if (preview) preview.hidden = true;
        const field = byId("dm-reply-to-id", HTMLInputElement);
        if (field) field.value = "";
    }

    private replyTo(control: HTMLElement): void {
        closeBubbleMenus();
        const preview = document.getElementById("dm-reply-preview");
        if (!preview) return;
        const name = document.getElementById("dm-reply-preview-name");
        const snippet = document.getElementById("dm-reply-preview-snippet");
        if (name) name.textContent = control.dataset.replyName ?? "";
        if (snippet) snippet.textContent = control.dataset.replyPreview ?? "";
        const field = byId("dm-reply-to-id", HTMLInputElement);
        if (field) field.value = control.dataset.dmReply ?? "";
        preview.hidden = false;
        composerInput()?.focus();
    }

    private onSubmit(e: SubmitEvent): void {
        const form = e.target;
        if (!(form instanceof HTMLFormElement) || form.id !== "dm-composer") return;
        e.preventDefault();
        const input = composerInput();
        if (!input) return;
        const body = input.value.trim();
        const imageIds = this.pending.images.map((i) => i.id);
        const mapUuid = this.pending.mapUuid;
        const share = this.pending.share;
        const replyField = byId("dm-reply-to-id", HTMLInputElement);
        const replyTo = replyField?.value ? Number.parseInt(replyField.value, 10) : null;
        if (!body && this.pending.isEmpty) return;
        const partner = activeSlug();
        const group = activeGroup();

        // A share creates a row the socket consumer has no logic for, so it goes over its own endpoint, carrying
        // the composer's text and any map with it. Not encrypted, like maps and photos.
        if (share) {
            void this.sendShare(form, share, body, group, partner, mapUuid);
            return;
        }

        // Only the text is encrypted; attachments stay server-visible.
        void this.outgoingContent(partner, body).then((content) => {
            // Encryption failed and has said so; the composer keeps the text and attachments for a retry.
            if (!content) return;
            const frame: Record<string, unknown> = { body: content.body, ciphertext: content.ciphertext ?? "", nonce: content.nonce ?? "", key_version: content.keyVersion ?? 0 };
            if (group) frame.group = group;
            else Object.assign(frame, { recipient: partner, image_ids: imageIds, markup_map_uuid: mapUuid, reply_to: replyTo });
            if (this.socket?.send(frame)) {
                resetComposerInput(input);
                input.focus();
                this.pending.reset();
                this.cancelReply();
                return;
            }
            void this.postMessage(form, input, body, content, imageIds);
        });
    }

    /** The socket is down: POST instead, and show the thread the server answers with. The text comes back on failure. */
    private async postMessage(form: HTMLFormElement, input: HTMLTextAreaElement, body: string, content: OutgoingContent, imageIds: number[]): Promise<void> {
        const data = new FormData(form);
        data.set("body", content.body);
        if (content.ciphertext) {
            data.set("ciphertext", content.ciphertext);
            data.set("nonce", content.nonce ?? "");
            data.set("key_version", String(content.keyVersion ?? 0));
        }
        imageIds.forEach((id) => data.append("image_ids", String(id)));
        resetComposerInput(input);
        try {
            replacePane(await fetchText(form.action, { method: "POST", body: data, reportsItsOwnErrors: true }));
            this.pending.reset();
            this.cancelReply();
            scrollMessages();
            refreshList();
            this.refreshEncryption();
        } catch (err) {
            input.value = body;
            autoExpand(input);
            toast.error(failureMessage(err, "Message failed to send. Please try again."));
        }
    }

    private shareUrl(partner: string, kind: string): string {
        return this.urls.share.replace(SLUG_TOKEN, encodeURIComponent(partner)).replace(/\/share\/pin\/$/, `/share/${encodeURIComponent(kind)}/`);
    }

    private async sendShare(form: HTMLFormElement, share: StagedShare, body: string, group: string, partner: string, mapUuid: string | null): Promise<void> {
        const sendBtn = form.querySelector<HTMLButtonElement>(".dm-composer__send");
        const data = new FormData();
        data.append("csrfmiddlewaretoken", getCsrfToken());
        data.append(share.fieldName, share.value);
        data.append("body", body);
        if (!group && share.kind === "pin" && mapUuid) data.append("markup_map_uuid", mapUuid);
        const url = group ? this.urls.groupSharePin.replace(UUID_TOKEN, group) : this.shareUrl(partner, share.kind);
        if (sendBtn) sendBtn.disabled = true;
        try {
            replacePane(await fetchText(url, { method: "POST", body: data, credentials: "same-origin", reportsItsOwnErrors: true }));
            resetComposerInput(composerInput());
            this.pending.reset();
            this.cancelReply();
            scrollMessages();
            refreshList();
            this.refreshEncryption();
        } catch (err) {
            toast.error(failureMessage(err, "Could not send that share."));
        } finally {
            if (sendBtn) sendBtn.disabled = false;
        }
    }

    // -- The open thread -------------------------------------------------------------------------

    private installThreadClicks(): void {
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            if (!el) return;
            const menuBtn = el.closest<HTMLElement>(".dm-bubble__menu-btn");
            const partnerMenuBtn = el.closest("#dm-partner-menu-btn");
            if (menuBtn || partnerMenuBtn) {
                const menu = document.getElementById(menuBtn ? `dm-bubble-menu-${menuBtn.dataset.messageId}` : "dm-partner-menu");
                closeBubbleMenus(menu);
                if (menu) menu.hidden = !menu.hidden;
                return;
            }
            const reply = el.closest<HTMLElement>("[data-dm-reply]");
            if (reply) {
                this.replyTo(reply);
                return;
            }
            if (el.closest("#dm-reply-preview-cancel")) {
                this.cancelReply();
                return;
            }
            if (el.closest("#dm-thread-search-btn")) {
                this.toggleThreadSearch();
                return;
            }
            if (el.closest("#dm-thread-search-close")) {
                this.closeThreadSearch();
                return;
            }
            if (el.closest(".dm-search-result")) {
                // The link navigates or jumps on its own; this only tidies the panels away.
                this.closeThreadSearch();
                this.closeMessagesSearch();
            }
            if (!el.closest(".dm-bubble-menu")) closeBubbleMenus();

            // Tapping a bubble, not a control in it, shows its actions and time where there is no hover.
            const bubble = el.closest(".dm-bubble");
            const peeking = () => document.querySelectorAll(".dm-bubble--peek").forEach((b) => b.classList.remove("dm-bubble--peek"));
            if (bubble && !el.closest("button, a, .dm-reactions, .dm-bubble-menu, .dm-bubble__image-link")) {
                const wasPeek = bubble.classList.contains("dm-bubble--peek");
                peeking();
                if (!wasPeek) bubble.classList.add("dm-bubble--peek");
                return;
            }
            if (!bubble) peeking();
        });
    }

    private reactUrl(messageId: number): string {
        return this.urls.react.replace(SLUG_TOKEN, encodeURIComponent(activeSlug())).replace(/\/react\/0\/$/, `/react/${messageId}/`);
    }

    private updateReactionBar(messageId: number, reactions: DmReaction[]): void {
        const container = document.getElementById(`dm-reactions-${messageId}`);
        if (!container) return;
        renderReactionBar(container, messageId, this.reactUrl(messageId), this.mySlug, reactions);
        window.htmx?.process(container);
    }

    private appendBubble(msg: DmMessage, own: boolean): void {
        const list = document.getElementById("dm-messages");
        if (!list) return;
        const empty = document.getElementById("dm-thread-empty");
        if (empty) empty.hidden = true;
        const li = buildBubble(list, msg, own);
        list.appendChild(li);
        if (!msg.groupUuid) this.updateReactionBar(msg.id, []);
        if (msg.ciphertext) this.e2ee?.decryptDom(li, activeSlug()).catch((err: unknown) => console.error("E2EE decrypt failed", err));
        scrollMessages();
    }

    private async refreshActiveThread(): Promise<void> {
        const url = activeThread()?.dataset.viewUrl;
        if (!url || !window.htmx) return;
        await window.htmx.ajax("GET", url, { target: "#dm-thread-pane", swap: "innerHTML" });
        window._initThumbs?.();
        scrollMessages();
        this.refreshEncryption();
    }

    private markThreadRead(): void {
        const url = activeThread()?.dataset.readUrl;
        if (!url) return;
        fetchText(url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, reportsItsOwnErrors: true })
            .then(() => window.htmx?.trigger(document.body, "msgCountRefresh"))
            // The badge catches up on its next poll.
            .catch(() => undefined);
    }

    // -- Live delivery ---------------------------------------------------------------------------

    private setConnected(connected: boolean): void {
        const status = document.getElementById("dm-conn-status");
        if (status) status.hidden = connected;
    }

    /** Tells the server this conversation is on screen, so it skips notifying about it (``is_thread_open``). */
    private sendOpenThread(): void {
        const group = activeGroup();
        const partner = activeSlug();
        if (group) this.socket?.send({ type: "open", group });
        else if (partner) this.socket?.send({ type: "open", recipient: partner });
    }

    private maybeSendTyping(): void {
        const now = Date.now();
        if (now - this.lastTypingSentAt < 3000) return;
        this.lastTypingSentAt = now;
        const partner = activeSlug();
        if (partner) this.socket?.send({ type: "typing", recipient: partner });
    }

    private showTypingIndicator(): void {
        const el = document.getElementById("dm-partner-typing");
        if (!el) return;
        el.hidden = false;
        clearTimeout(this.typingHideTimer);
        this.typingHideTimer = setTimeout(() => (el.hidden = true), 4000);
    }

    private connect(): void {
        this.setConnected(false);
        this.socket = openLiveSocket({
            path: "/ws/messages/",
            onOpen: () => {
                this.reconnects = 0;
                this.setConnected(true);
                this.sendOpenThread();
            },
            onClose: () => {
                this.setConnected(false);
                this.reconnects += 1;
                if (this.reconnects === SLOW_RECONNECT_WARNING_AFTER) toast.warning("Still trying to reconnect - messages may be delayed.");
            },
            onMessage: (data) => {
                const frame = parseDmFrame(data);
                if (frame) this.onFrame(frame);
            },
        });
        // The open marker has a server-side TTL; the poll pauses in a hidden tab, so an unwatched conversation notifies again.
        startPoller(() => this.sendOpenThread(), { intervalMs: 60000 });
    }

    private onFrame(frame: DmFrame): void {
        switch (frame.type) {
            case "error":
                toast.error(frame.detail || "Your message couldn't be sent.");
                return;
            case "reaction":
                this.updateReactionBar(frame.messageId, frame.reactions);
                return;
            case "message_deleted": {
                const bubble = document.querySelector<HTMLElement>(`.dm-bubble[data-message-id="${frame.messageId}"]`);
                // The sender's own bubble for an everyone-deletion is already tombstoned by the response to their request.
                if (bubble && !(frame.everyone && bubble.classList.contains("dm-bubble--own"))) tombstone(bubble, frame.everyone ? "Message deleted" : "You removed this message");
                return;
            }
            case "typing":
                if (frame.senderSlug === activeSlug()) this.showTypingIndicator();
                return;
            case "group_message": {
                const msg = frame.message;
                if (msg.groupUuid === activeGroup()) {
                    if (msg.hasShare) void this.refreshActiveThread();
                    else this.appendBubble(msg, msg.senderSlug === this.mySlug);
                    if (msg.senderSlug !== this.mySlug) this.markThreadRead();
                }
                refreshList();
                return;
            }
            case "group_updated":
                // Renamed, or this viewer removed: the re-fetch of the open group handles both.
                refreshList();
                if (frame.groupUuid === activeGroup()) void this.refreshActiveThread();
                return;
            case "group_message_deleted": {
                if (frame.groupUuid !== activeGroup()) return;
                const bubble = document.querySelector<HTMLElement>(`.dm-bubble[data-message-id="${frame.messageId}"]`);
                if (bubble && !bubble.classList.contains("dm-bubble--own")) tombstone(bubble, "Message deleted");
                return;
            }
            case "message":
                this.onDirectMessage(frame.message, frame.recipientSlug);
        }
    }

    private onDirectMessage(msg: DmMessage, recipientSlug: string): void {
        const active = activeSlug();
        const incoming = msg.senderSlug === active;
        // Our own message, from this tab or another, echoing back.
        const echo = msg.senderSlug === this.mySlug && recipientSlug === active;
        if (!incoming && !echo) {
            refreshList();
            return;
        }
        const own = !incoming;
        if (needsServerRender(msg, own)) void this.refreshActiveThread();
        else this.appendBubble(msg, own);
        if (incoming) this.markThreadRead();
        window.htmx?.trigger(document.body, "dmListRefresh");
    }

    // -- Groups ----------------------------------------------------------------------------------

    private async openHostedDialog(url: string): Promise<void> {
        try {
            const html = await fetchText(url, { credentials: "same-origin", reportsItsOwnErrors: true });
            const host = document.getElementById("dm-share-dialog-host");
            if (!host) return;
            host.innerHTML = html;
            window.htmx?.process(host);
            host.querySelector("dialog")?.showModal();
        } catch {
            toast.error("Could not load that dialog. Please try again.");
        }
    }

    private installGroupControls(): void {
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            const group = activeGroup();
            if (el?.closest("#dm-group-members-btn")) {
                closeBubbleMenus();
                if (group) void this.openHostedDialog(this.urls.groupMembers.replace(UUID_TOKEN, group));
            } else if (el?.closest("#dm-group-share-pin-btn")) {
                if (group) void this.openHostedDialog(this.urls.groupSharePin.replace(UUID_TOKEN, group));
            } else if (el?.closest("#dm-group-rename-btn")) {
                closeBubbleMenus();
                const form = document.getElementById("dm-group-rename-form");
                if (!form) return;
                form.hidden = false;
                const input = byId("dm-group-rename-input", HTMLInputElement);
                input?.focus();
                input?.select();
            } else if (el?.closest("#dm-group-rename-cancel")) {
                const form = document.getElementById("dm-group-rename-form");
                if (form) form.hidden = true;
            } else {
                const pick = el?.closest<HTMLElement>(".dm-group-member-pick");
                if (pick) {
                    e.preventDefault();
                    this.pickMember(pick);
                } else if (el?.closest("#dm-group-create-submit")) {
                    void this.createGroup();
                }
            }
        });
        installTogglePanel(document.getElementById("dm-group-create-btn"), document.getElementById("dm-group-create"), "dm-group-name-input", false);
    }

    /**
     * A member search result adds a removable chip, in the new-group panel or the members dialog's add form. The
     * form's chips carry a hidden ``member_slugs`` input; the panel's are read when it submits.
     */
    private pickMember(pick: HTMLElement): void {
        const inAddForm = !!pick.closest("#dm-group-add-members-form");
        const chips = document.getElementById(inAddForm ? "dm-group-add-member-picked" : "dm-group-create-picked");
        const submit = byId(inAddForm ? "dm-group-add-members-submit" : "dm-group-create-submit", HTMLButtonElement);
        const slug = pick.dataset.slug;
        if (!chips || !slug || Array.from(chips.children).some((c) => c instanceof HTMLElement && c.dataset.slug === slug)) return;
        const sync = (): void => {
            if (submit) submit.disabled = chips.children.length === 0;
        };
        const chip = document.createElement("span");
        chip.className = "dm-group-picked-chip";
        chip.dataset.slug = slug;
        const name = document.createElement("span");
        name.textContent = pick.dataset.name || slug;
        chip.appendChild(name);
        if (inAddForm) {
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "member_slugs";
            hidden.value = slug;
            chip.appendChild(hidden);
        }
        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove");
        remove.textContent = "×";
        remove.addEventListener("click", () => {
            chip.remove();
            sync();
        });
        chip.appendChild(remove);
        chips.appendChild(chip);
        sync();
    }

    private async createGroup(): Promise<void> {
        const nameInput = byId("dm-group-name-input", HTMLInputElement);
        const picked = document.getElementById("dm-group-create-picked");
        const submit = byId("dm-group-create-submit", HTMLButtonElement);
        if (!nameInput || !picked) return;
        const name = nameInput.value.trim();
        if (!name) {
            nameInput.focus();
            toast.warning("Give your group a name first.");
            return;
        }
        const data = new FormData();
        data.append("csrfmiddlewaretoken", getCsrfToken());
        data.append("name", name);
        Array.from(picked.children).forEach((chip) => {
            if (chip instanceof HTMLElement && chip.dataset.slug) data.append("member_slugs", chip.dataset.slug);
        });
        if (submit) submit.disabled = true;
        try {
            const created = await fetchJson(this.urls.groupCreate, { method: "POST", body: data, credentials: "same-origin", headers: { "X-CSRFToken": getCsrfToken() }, reportsItsOwnErrors: true });
            const url = typeof created === "object" && created !== null && "url" in created && typeof created.url === "string" ? created.url : "";
            nameInput.value = "";
            picked.replaceChildren();
            document.getElementById("dm-group-member-results")?.replaceChildren();
            const memberInput = byId("dm-group-member-input", HTMLInputElement);
            if (memberInput) memberInput.value = "";
            const panel = document.getElementById("dm-group-create");
            if (panel) panel.hidden = true;
            refreshList();
            if (url && window.htmx) {
                await window.htmx.ajax("GET", url, { target: "#dm-thread-pane", swap: "innerHTML" });
                history.pushState({}, "", url);
            }
        } catch (err) {
            toast.error(failureMessage(err, "Could not create the group."));
        } finally {
            if (submit) submit.disabled = picked.children.length === 0;
        }
    }

    // -- Attachments -----------------------------------------------------------------------------

    private mapComposerOptions(startTab: "draw" | "existing" = "draw"): CommentMapComposerOptions {
        const share = this.pending.share;
        return {
            onSaved: (uuid) => this.pending.setMap(uuid),
            existingMapPicker: { fetchUrl: this.urls.mapPicker, onPick: (uuid) => this.pending.setMap(uuid) },
            startTab,
            // Drawing a map to go with a staged pin starts on that pin rather than the world.
            initialView: share?.kind === "pin" && share.lat !== null && share.lng !== null ? { lat: share.lat, lng: share.lng, zoom: 15 } : null,
        };
    }

    private async uploadPhoto(file: File): Promise<void> {
        const data = new FormData();
        data.append("image", file);
        try {
            const result = await fetchJson(this.urls.uploadImage, {
                method: "POST",
                body: data,
                credentials: "same-origin",
                headers: { "X-CSRFToken": getCsrfToken() },
                // A large photo on a slow link outlasts the default.
                timeoutMs: 10 * 60 * 1000,
                reportsItsOwnErrors: true,
            });
            if (typeof result !== "object" || result === null || !("id" in result) || typeof result.id !== "number") throw new Error("no id");
            const url = "url" in result && typeof result.url === "string" ? result.url : "";
            this.pending.addImage({ id: result.id, url, failed: false }, "processing" in result && result.processing === true);
        } catch (err) {
            toast.error(failureMessage(err, "Photo upload failed."));
        }
    }

    /** Opens a received map in the composer and attaches the edited copy; the original is never changed. */
    private async editReceivedMap(btn: HTMLElement): Promise<void> {
        const uuid = btn.dataset.mapUuid;
        const template = activeThread()?.dataset.markupSnapshotUrlTemplate;
        if (!uuid || !template) return;
        try {
            const data = await fetchJson(template.replace(UUID_TOKEN, uuid), { credentials: "same-origin", reportsItsOwnErrors: true });
            if (!data) throw new Error("empty snapshot");
            window._openCommentMapComposer({
                existingData: data,
                onSaved: (newUuid) => {
                    this.pending.setMap(newUuid);
                    composerInput()?.focus();
                },
            });
        } catch {
            toast.error("Could not load that map.");
        }
    }

    private installAttachments(): void {
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            if (!el) return;
            if (el.closest("#dm-attach-photo-btn")) document.getElementById("dm-attach-photo-input")?.click();
            else if (el.closest("#dm-attach-map-btn")) window._openCommentMapComposer(this.mapComposerOptions());
            else if (el.closest("#dm-map-chip-remove")) this.pending.setMap(null);
            else if (el.closest("#dm-share-chip-remove")) this.pending.setShare(null);
            else {
                const edit = el.closest<HTMLElement>(".dm-map-edit-btn");
                if (edit) void this.editReceivedMap(edit);
            }
        });
        document.addEventListener("change", (e) => {
            const input = e.target;
            if (!(input instanceof HTMLInputElement) || input.id !== "dm-attach-photo-input") return;
            const files = Array.from(input.files ?? []);
            input.value = "";
            files.forEach((file) => void this.uploadPhoto(file));
        });
    }

    // -- @ actions, share dialogs and emoji ------------------------------------------------------

    private onMentionPicked(kind: MentionKind): void {
        if (kind === "map") window._openCommentMapComposer(this.mapComposerOptions("existing"));
        else void this.openShareDialog(kind);
    }

    private async openShareDialog(kind: string): Promise<void> {
        const partner = activeSlug();
        if (partner) await this.openHostedDialog(this.shareUrl(partner, kind));
    }

    private openComposerEmoji(button: HTMLElement): void {
        const existing = document.getElementById("dm-composer-emoji-popover");
        if (existing) {
            existing.remove();
            return;
        }
        closeEmojiPopovers();
        const popover = document.createElement("div");
        popover.id = "dm-composer-emoji-popover";
        popover.className = "dm-emoji-popover dm-emoji-popover--composer";
        for (const emoji of INSERT_EMOJIS) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = emoji;
            btn.addEventListener("click", () => {
                const input = composerInput();
                if (input) {
                    input.value += emoji;
                    autoExpand(input);
                    input.focus();
                }
                popover.remove();
            });
            popover.appendChild(btn);
        }
        button.after(popover);
    }

    private installMentionAndEmoji(): void {
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            if (!el) return;
            if (el.closest("#dm-mention-btn")) {
                if (this.mention.isOpen) this.mention.close();
                else {
                    closeEmojiPopovers();
                    this.mention.insertTrigger();
                }
                return;
            }
            if (!el.closest("#dm-mention-menu")) this.mention.close();
        });
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            if (!el) return;
            const emojiBtn = el.closest<HTMLElement>("#dm-emoji-btn");
            if (emojiBtn) {
                this.openComposerEmoji(emojiBtn);
                return;
            }
            if (!el.closest(".dm-emoji-popover") && !el.closest(".dm-reaction-add-btn")) {
                closeEmojiPopovers();
                document.getElementById("dm-composer-emoji-popover")?.remove();
            }
        });
        // A pick stages the share as a chip; it is sent with the message, so it cannot go before the text is done.
        document.addEventListener("click", (e) => {
            const pick = elementOf(e)?.closest<HTMLElement>(".dm-share-dialog button.dm-share-pick-btn");
            const share = pick ? shareFromPick(pick) : null;
            if (!pick || !share) return;
            this.pending.setShare(share);
            pick.closest("dialog")?.close();
            composerInput()?.focus();
        });
        document.addEventListener("input", (e) => {
            const filter = e.target;
            if (!(filter instanceof HTMLInputElement) || !filter.classList.contains("dm-share-filter")) return;
            const query = filter.value.trim().toLowerCase();
            filter.parentElement?.querySelectorAll<HTMLElement>(".dm-share-list .dm-share-list-item").forEach((item) => {
                item.hidden = !!query && !(item.dataset.search ?? "").includes(query);
            });
        });
    }

    // -- Sidebar ---------------------------------------------------------------------------------

    private installSidebar(): void {
        const layout = document.getElementById("dm-layout");
        const collapseBtn = document.getElementById("dm-sidebar-collapse-btn");
        const expandBtn = document.getElementById("dm-sidebar-expand-btn");
        if (layout && collapseBtn && expandBtn) {
            const setCollapsed = (collapsed: boolean): void => {
                layout.classList.toggle("dm-layout--sidebar-collapsed", collapsed);
                expandBtn.hidden = !collapsed;
                try {
                    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
                } catch {
                    /* Storage can be blocked; the sidebar still toggles. */
                }
            };
            collapseBtn.addEventListener("click", () => setCollapsed(true));
            expandBtn.addEventListener("click", () => setCollapsed(false));
            let stored: string | null = null;
            try {
                stored = localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
            } catch {
                /* As above. */
            }
            if (stored === "1") setCollapsed(true);
        }
        installTogglePanel(document.getElementById("dm-compose-btn"), document.getElementById("dm-compose"), "dm-recipient-input", true);
        this.closeMessagesSearch = installTogglePanel(document.getElementById("dm-messages-search-btn"), document.getElementById("dm-messages-search"), "dm-messages-search-input", true) ?? (() => undefined);
    }

    private toggleThreadSearch(): void {
        const panel = document.getElementById("dm-thread-search");
        const btn = document.getElementById("dm-thread-search-btn");
        if (!panel || !btn) return;
        const open = !panel.hidden;
        panel.hidden = open;
        btn.setAttribute("aria-expanded", String(!open));
        if (!open) document.getElementById("dm-thread-search-input")?.focus();
    }

    private closeThreadSearch(): void {
        const panel = document.getElementById("dm-thread-search");
        if (!panel || panel.hidden) return;
        panel.hidden = true;
        document.getElementById("dm-thread-search-btn")?.setAttribute("aria-expanded", "false");
    }

    private installThreadSearch(): void {
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") this.closeThreadSearch();
        });
    }

    // -- Swaps, history and deep links -----------------------------------------------------------

    /**
     * Scroll to and flash a message named by the URL hash. Only the newest page of history renders at first, so
     * an older target is reached by pulling earlier pages through the infinite-scroll sentinel until it appears.
     */
    private ensureMessageVisible(id: string, hopsLeft = 40): void {
        const target = document.getElementById(`dm-msg-${id}`);
        if (target) {
            target.scrollIntoView({ block: "center" });
            target.classList.add("dm-bubble--highlight");
            setTimeout(() => target.classList.remove("dm-bubble--highlight"), 2000);
            return;
        }
        const sentinel = document.getElementById("dm-load-older-sentinel");
        if (hopsLeft <= 0 || !sentinel || !window.htmx) return;
        const onOlderSwap = (evt: Event): void => {
            if (htmxDetail(evt).elt !== sentinel) return;
            document.body.removeEventListener("htmx:afterSwap", onOlderSwap);
            this.ensureMessageVisible(id, hopsLeft - 1);
        };
        document.body.addEventListener("htmx:afterSwap", onOlderSwap);
        window.htmx.trigger(sentinel, "revealed");
    }

    private highlightMessageFromHash(): void {
        const hash = window.location.hash;
        if (hash.startsWith("#dm-msg-")) this.ensureMessageVisible(hash.slice("#dm-msg-".length));
    }

    private installSwaps(): void {
        document.body.addEventListener("htmx:afterSwap", (ev) => {
            const target = htmxDetail(ev).target;
            if (!target) return;
            if (target.id === "dm-thread-pane") {
                // On a phone, opening a conversation shows the thread in place of the list.
                this.page.classList.add("dm-page--thread-open");
                syncActiveConversationHighlight();
                this.pending.reset();
                this.cancelReply();
                scrollMessages();
                this.sendOpenThread();
                this.refreshEncryption();
                const input = composerInput();
                autoExpand(input);
                input?.focus();
            } else if (target.id === "dm-conversation-list" || target.querySelector("#dm-conversation-list")) {
                this.decryptSidebarPreviews();
            }
        });
        // Older history is prepended above the reader, and browsers do not adjust scrollTop for it: put back the
        // height inserted so the view stays on what they were reading.
        document.body.addEventListener("htmx:beforeRequest", (evt) => {
            if (htmxDetail(evt).elt?.id !== "dm-load-older-sentinel") return;
            this.olderScrollHeight = document.getElementById("dm-messages")?.scrollHeight ?? null;
        });
        document.body.addEventListener("htmx:afterSettle", (evt) => {
            if (htmxDetail(evt).elt?.id !== "dm-load-older-sentinel" || this.olderScrollHeight === null) return;
            const list = document.getElementById("dm-messages");
            if (list) list.scrollTop += list.scrollHeight - this.olderScrollHeight;
            window._initThumbs?.();
            this.refreshEncryption();
        });
        document.body.addEventListener("htmx:afterRequest", (evt) => {
            if (htmxDetail(evt).elt?.id === "dm-load-older-sentinel") this.olderScrollHeight = null;
        });
    }

    // -- Transcript and unlock -------------------------------------------------------------------

    /** A plaintext transcript of the open thread, from the already-decrypted bubbles: the only readable copy there is. */
    private downloadTranscript(): void {
        const thread = activeThread();
        if (!thread) return;
        const partner = thread.dataset.partnerSlug ?? "";
        const lines: string[] = [];
        thread.querySelectorAll(".dm-bubble").forEach((bubble) => {
            const who = bubble.classList.contains("dm-bubble--own") ? "You" : partner;
            const body = bubble.querySelector(".dm-bubble__body")?.textContent?.trim();
            const removed = bubble.querySelector(".dm-bubble__tombstone")?.textContent?.trim();
            const text = body ?? (removed ? `[${removed}]` : "");
            const time = bubble.querySelector(".dm-bubble__time")?.textContent?.trim() ?? "";
            if (text) lines.push(`${time ? `[${time}] ` : ""}${who}: ${text}`);
        });
        const blob = new Blob([`Conversation with ${partner}\n\n${lines.join("\n")}\n`], { type: "text/plain" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `urbanlens-conversation-${partner}.txt`;
        link.click();
        URL.revokeObjectURL(link.href);
    }

    private installThreadActions(): void {
        document.addEventListener("click", (e) => {
            const el = elementOf(e);
            if (el?.closest("#dm-download-transcript")) {
                this.downloadTranscript();
                return;
            }
            const e2ee = this.e2ee;
            if (!el?.closest("#dm-locked-unlock-btn") || !e2ee) return;
            void e2ee.showUnlockDialog().then((ok) => {
                if (!ok) return;
                this.refreshEncryption();
                void this.refreshActiveThread();
                this.decryptSidebarPreviews();
                toast.success("This device is unlocked.");
            });
        });
    }
}

const page = byId("dm-page", HTMLElement);
if (page) new MessagesPage(page).install();
