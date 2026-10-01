/**
 * The open conversation's pane (``partials/messages/_thread.html`` and ``_group_thread.html``).
 */

/**
 * Put the server's rendering of the thread's messages in place of the open one's, leaving everything else,
 * the composer's draft, reply and attachments included, as the viewer left it.
 *
 * Returns false, changing nothing, when *html* has no message list.
 */
export function replaceThreadMessages(html: string): boolean {
    const list = document.getElementById("dm-messages");
    const fresh = new DOMParser().parseFromString(html, "text/html");
    const freshList = fresh.getElementById("dm-messages");
    if (!list || !freshList) return false;
    list.replaceChildren(...Array.from(freshList.childNodes, (node) => document.importNode(node, true)));
    const empty = document.getElementById("dm-thread-empty");
    const freshEmpty = fresh.getElementById("dm-thread-empty");
    if (empty && freshEmpty) empty.hidden = freshEmpty.hidden;
    window.htmx?.process(list);
    return true;
}
