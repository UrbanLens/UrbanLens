/**
 * Validation for colours that get interpolated into `style="…"` strings.
 */

/** Hex forms the server's `COLOR_CHOICES` uses, plus the 3-digit shorthand. */
const HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/**
 * The colour when it is a plain hex value, `""` otherwise.
 */
export function safeColor(value: string | null | undefined): string {
    return value && HEX_COLOR_RE.test(value) ? value : "";
}
