/**
 * Validation for colours that get interpolated into `style="…"` strings.
 *
 * Label and category colours reach the browser as `data-*` attributes and are read back
 * through `dataset`, which returns the *decoded* value - Django escaped the colour when
 * rendering the attribute, but that escaping does not survive the round trip. A stored
 * colour such as `x" onmouseover="…` therefore arrives in JS with its quotes intact, and
 * every consumer builds markup by interpolating it into a `style` attribute and handing
 * the result to `innerHTML`/`insertAdjacentHTML`.
 *
 * The stored value cannot be assumed to be a colour: `Label.color` declares `choices`,
 * which Django enforces only in `full_clean()`, and the label write paths assign it
 * straight from request data (`request.POST.get("color")`).
 *
 * Validated rather than escaped on purpose - escaping stops the attribute breakout but
 * still permits CSS injection (`url(...)` and friends) inside the style value.
 */

/** Hex forms the server's `COLOR_CHOICES` uses, plus the 3-digit shorthand. */
const HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/**
 * The colour when it is a plain hex value, `""` otherwise.
 *
 * Callers treat `""` as "no colour set" and fall back to their own default, which is the
 * same branch an absent colour already took.
 */
export function safeColor(value: string | null | undefined): string {
    return value && HEX_COLOR_RE.test(value) ? value : "";
}
