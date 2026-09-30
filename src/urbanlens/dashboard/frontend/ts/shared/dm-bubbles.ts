/**
 * Live message bubbles for the Messages page, built from a socket frame to match ``_message_items.html``.
 *
 * Everything a peer controls (body, names, reaction emoji) goes in as text or through ``setAttribute``/
 * ``JSON.stringify``, never as markup.
 */

import type { DmMessage, DmReaction } from "./dm-frames";
import { processingPlaceholder } from "./photo-processing";

/** Mirrors ``services.direct_messages.MESSAGE_GROUP_GAP_SECONDS``. */
const MESSAGE_GROUP_GAP_MS = 5 * 60 * 1000;

/** Mirrors ``services.direct_messages.REACTION_PICKER_EMOJIS``. */
export const REACT_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉"];

/** Whether a message continues the thread's last bubble: same side, same day, within the gap. */
export function isGroupedWithPrevious(list: Element, own: boolean, createdIso: string): boolean {
    const bubbles = list.querySelectorAll<HTMLElement>(".dm-bubble");
    const last = bubbles[bubbles.length - 1];
    if (!last || last.classList.contains("dm-bubble--own") !== own) return false;
    const lastTime = new Date(last.dataset.createdAt ?? "");
    const thisTime = new Date(createdIso);
    if (Number.isNaN(lastTime.getTime()) || Number.isNaN(thisTime.getTime())) return false;
    if (lastTime.toDateString() !== thisTime.toDateString()) return false;
    const gap = thisTime.getTime() - lastTime.getTime();
    return gap >= 0 && gap < MESSAGE_GROUP_GAP_MS;
}

/**
 * A map, a share, a location mention or someone else's photo needs the server's render (the map viewer, the share
 * card, the photo-consent decision), so the thread is re-fetched rather than built here.
 */
export function needsServerRender(msg: DmMessage, own: boolean): boolean {
    return !!msg.markupMapUuid || msg.hasShare || msg.hasLocationMentions || (msg.images.length > 0 && !own);
}

function span(className: string, text: string): HTMLSpanElement {
    const el = document.createElement("span");
    el.className = className;
    el.textContent = text;
    return el;
}

function setCipher(el: HTMLElement, ciphertext: string, nonce: string, keyVersion: number): void {
    el.dataset.e2eeCt = ciphertext;
    el.dataset.e2eeNonce = nonce;
    el.dataset.e2eeKv = String(keyVersion);
}

function imagesOf(msg: DmMessage): HTMLDivElement {
    const wrap = document.createElement("div");
    wrap.className = "dm-bubble__images";
    for (const img of msg.images) {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "dm-bubble__image-link";
        if (img.url) {
            const thumb = document.createElement("img");
            thumb.src = img.url;
            thumb.alt = "";
            thumb.className = "dm-bubble__image";
            link.appendChild(thumb);
        } else {
            // The thread pane's processing observer swaps the photo in once it lands.
            link.disabled = true;
            link.dataset.processingOpen = "";
            link.dataset.id = String(img.id);
            link.dataset.processing = img.processingFailed ? "failed" : "pending";
            link.appendChild(processingPlaceholder("dm-bubble__image dm-bubble__image--processing", img.processingFailed));
        }
        wrap.appendChild(link);
    }
    return wrap;
}

/** The bubble for *msg*, grouped with the thread's last one when it continues it. Its reaction bar is left empty. */
export function buildBubble(list: Element, msg: DmMessage, own: boolean): HTMLLIElement {
    const inGroup = !!msg.groupUuid;
    const li = document.createElement("li");
    li.className = `dm-bubble ${own ? "dm-bubble--own" : "dm-bubble--them"}`;
    if (isGroupedWithPrevious(list, own, msg.created)) li.classList.add("dm-bubble--grouped");
    li.id = `dm-msg-${msg.id}`;
    li.dataset.messageId = String(msg.id);
    li.dataset.createdAt = msg.created;

    if (inGroup && !own) li.appendChild(span("dm-bubble__sender", msg.senderName));

    if (msg.replyTo) {
        const quote = document.createElement("div");
        quote.className = "dm-quote-box";
        const snippet = span("dm-quote-box__snippet", msg.replyTo.preview);
        if (msg.replyTo.ciphertext) {
            setCipher(snippet, msg.replyTo.ciphertext, msg.replyTo.nonce, msg.replyTo.keyVersion);
            snippet.dataset.e2eeTruncate = "80";
        }
        quote.append(span("dm-quote-box__author", msg.replyTo.senderName), snippet);
        li.appendChild(quote);
    }

    if (msg.ciphertext) {
        const body = span("dm-bubble__body e2ee-pending", "Decrypting…");
        setCipher(body, msg.ciphertext, msg.nonce, msg.keyVersion);
        if (inGroup) body.dataset.e2eeGroup = msg.groupUuid;
        const lock = document.createElement("i");
        lock.className = "material-symbols-outlined dm-lock-icon";
        lock.title = "End-to-end encrypted";
        lock.textContent = "lock";
        li.append(body, lock);
    } else if (msg.body) {
        li.appendChild(span("dm-bubble__body", msg.body));
    }

    if (msg.images.length) li.appendChild(imagesOf(msg));

    const created = new Date(msg.created);
    li.appendChild(span("dm-bubble__time", Number.isNaN(created.getTime()) ? "" : created.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })));

    // Reactions are 1:1 only for now.
    if (!inGroup) {
        const reactions = document.createElement("div");
        reactions.className = "dm-reactions";
        reactions.id = `dm-reactions-${msg.id}`;
        li.appendChild(reactions);
    }
    return li;
}

function reactButton(reactUrl: string, messageId: number, emoji: string): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("hx-post", reactUrl);
    btn.setAttribute("hx-vals", JSON.stringify({ emoji }));
    btn.setAttribute("hx-target", `#dm-reactions-${messageId}`);
    btn.setAttribute("hx-swap", "outerHTML");
    return btn;
}

/** Rebuild a reaction bar to match ``_message_reactions.html``. The caller hands it to ``htmx.process``. */
export function renderReactionBar(container: HTMLElement, messageId: number, reactUrl: string, mySlug: string, reactions: DmReaction[]): void {
    container.replaceChildren();
    for (const r of reactions) {
        const pill = reactButton(reactUrl, messageId, r.emoji);
        pill.className = r.slugs.includes(mySlug) ? "dm-reaction-pill dm-reaction-pill--mine" : "dm-reaction-pill";
        pill.append(span("dm-reaction-pill__emoji", r.emoji), span("dm-reaction-pill__count", String(r.count)));
        container.appendChild(pill);
    }
    const addWrap = span("dm-reaction-add-wrap", "");
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "dm-reaction-add-btn";
    addBtn.title = "Add a reaction";
    const icon = document.createElement("i");
    icon.className = "material-symbols-outlined";
    icon.textContent = "add_reaction";
    addBtn.appendChild(icon);
    const popover = span("dm-emoji-popover", "");
    popover.hidden = true;
    for (const emoji of REACT_EMOJIS) {
        const btn = reactButton(reactUrl, messageId, emoji);
        btn.textContent = emoji;
        popover.appendChild(btn);
    }
    addWrap.append(addBtn, popover);
    container.appendChild(addWrap);
}

/** Replace a bubble's content with the "deleted" marker. */
export function tombstone(bubble: HTMLElement, text: string): void {
    bubble.classList.add("dm-bubble--tombstone");
    const marker = span("dm-bubble__tombstone", ` ${text}`);
    const icon = document.createElement("i");
    icon.className = "material-symbols-outlined";
    icon.textContent = "block";
    marker.prepend(icon);
    bubble.replaceChildren(marker);
}

/**
 * The add-reaction toggle and its popover, for server-rendered and live-built bars alike. The composer's emoji
 * popover shares the class, so opening one closes the others.
 */
export function installReactionPopovers(): void {
    document.addEventListener("click", (e) => {
        const el = e.target instanceof Element ? e.target : null;
        const addBtn = el?.closest(".dm-reaction-add-btn");
        if (addBtn) {
            const popover = addBtn.nextElementSibling;
            if (!(popover instanceof HTMLElement)) return;
            closeEmojiPopovers(popover);
            popover.hidden = !popover.hidden;
            return;
        }
        const picked = el?.closest(".dm-reactions .dm-emoji-popover button")?.closest<HTMLElement>(".dm-emoji-popover");
        if (picked) picked.hidden = true;
    });
}

export function closeEmojiPopovers(except?: Element): void {
    document.querySelectorAll<HTMLElement>(".dm-emoji-popover").forEach((p) => {
        if (p !== except) p.hidden = true;
    });
}
