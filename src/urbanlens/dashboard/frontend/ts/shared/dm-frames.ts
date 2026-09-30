/**
 * The frames ``/ws/messages/`` pushes (``services/messaging/direct_messages.py``, ``group_chats.py``), checked on
 * arrival: a field of the wrong type reads as absent rather than reaching the DOM as something else.
 */

export interface DmImage {
    id: number;
    /** Empty until the upload's re-encode lands, and for a viewer not entitled to see it. */
    url: string;
    processingFailed: boolean;
}

export interface DmQuote {
    senderName: string;
    preview: string;
    ciphertext: string;
    nonce: string;
    keyVersion: number;
}

export interface DmMessage {
    id: number;
    created: string;
    senderSlug: string;
    senderName: string;
    body: string;
    ciphertext: string;
    nonce: string;
    keyVersion: number;
    images: DmImage[];
    replyTo: DmQuote | null;
    markupMapUuid: string;
    hasShare: boolean;
    hasLocationMentions: boolean;
    /** Empty for a 1:1 message. */
    groupUuid: string;
}

export interface DmReaction {
    emoji: string;
    count: number;
    slugs: string[];
}

export type DmFrame =
    | { type: "message"; message: DmMessage; recipientSlug: string }
    | { type: "group_message"; message: DmMessage }
    | { type: "reaction"; messageId: number; reactions: DmReaction[] }
    | { type: "message_deleted"; messageId: number; everyone: boolean }
    | { type: "group_message_deleted"; messageId: number; groupUuid: string }
    | { type: "group_updated"; groupUuid: string }
    | { type: "typing"; senderSlug: string }
    | { type: "error"; detail: string };

type Obj = Record<string, unknown>;

function isObj(value: unknown): value is Obj {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(o: Obj, key: string): string {
    const v = o[key];
    return typeof v === "string" ? v : "";
}

function num(o: Obj, key: string): number {
    const v = o[key];
    return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function objects(o: Obj, key: string): Obj[] {
    const v = o[key];
    return Array.isArray(v) ? v.filter(isObj) : [];
}

function parseImage(o: Obj): DmImage {
    return { id: num(o, "id"), url: str(o, "url"), processingFailed: o.processing_failed === true };
}

function parseQuote(o: Obj): DmQuote {
    return { senderName: str(o, "sender_name"), preview: str(o, "preview"), ciphertext: str(o, "ciphertext"), nonce: str(o, "nonce"), keyVersion: num(o, "key_version") };
}

function parseMessage(o: Obj): DmMessage {
    const replyTo = o.reply_to;
    return {
        id: num(o, "id"),
        created: str(o, "created"),
        senderSlug: str(o, "sender_slug"),
        senderName: str(o, "sender_name"),
        body: str(o, "body"),
        ciphertext: str(o, "ciphertext"),
        nonce: str(o, "nonce"),
        keyVersion: num(o, "key_version"),
        images: objects(o, "images").map(parseImage),
        replyTo: isObj(replyTo) ? parseQuote(replyTo) : null,
        markupMapUuid: str(o, "markup_map_uuid"),
        hasShare: o.has_share === true,
        hasLocationMentions: o.has_location_mentions === true,
        groupUuid: str(o, "group_uuid"),
    };
}

function parseReaction(o: Obj): DmReaction {
    const slugs = o.slugs;
    return { emoji: str(o, "emoji"), count: num(o, "count"), slugs: Array.isArray(slugs) ? slugs.filter((s): s is string => typeof s === "string") : [] };
}

/** The frame, or null for a type this page does not handle (the keep-alive among them). */
export function parseDmFrame(data: unknown): DmFrame | null {
    if (!isObj(data)) return null;
    switch (data.type) {
        case "message":
            return { type: "message", message: parseMessage(data), recipientSlug: str(data, "recipient_slug") };
        case "group_message":
            return { type: "group_message", message: parseMessage(data) };
        case "reaction":
            return { type: "reaction", messageId: num(data, "message_id"), reactions: objects(data, "reactions").map(parseReaction) };
        case "message_deleted":
            return { type: "message_deleted", messageId: num(data, "message_id"), everyone: data.scope === "everyone" };
        case "group_message_deleted":
            return { type: "group_message_deleted", messageId: num(data, "message_id"), groupUuid: str(data, "group_uuid") };
        case "group_updated":
            return { type: "group_updated", groupUuid: str(data, "group_uuid") };
        case "typing":
            return { type: "typing", senderSlug: str(data, "sender_slug") };
        case "error":
            return { type: "error", detail: str(data, "detail") };
        default:
            return null;
    }
}
