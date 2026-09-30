/**
 * The parts of an htmx event's ``detail`` a listener reads, checked rather than assumed.
 */

export interface HtmxEventDetail {
    /** The element that issued the request. */
    elt: Element | null;
    /** The element swapped into. */
    target: Element | null;
    /** ``htmx:configRequest``'s outgoing parameters, which a listener may add to. */
    parameters: Record<string, unknown> | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null;
}

function elementAt(detail: Record<string, unknown>, key: string): Element | null {
    const value = detail[key];
    return value instanceof Element ? value : null;
}

export function htmxDetail(event: Event): HtmxEventDetail {
    const detail: unknown = event instanceof CustomEvent ? event.detail : null;
    if (!isRecord(detail)) return { elt: null, target: null, parameters: null };
    const parameters = detail.parameters;
    return { elt: elementAt(detail, "elt"), target: elementAt(detail, "target"), parameters: isRecord(parameters) ? parameters : null };
}
