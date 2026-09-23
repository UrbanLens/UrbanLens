/**
 * Escape text for an HTML element body or a quoted attribute value.
 *
 * @param value - The text; non-strings are stringified.
 * @returns The text with `& < > " '` replaced by entities.
 */
export function escHtml(value: unknown): string {
    return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}
