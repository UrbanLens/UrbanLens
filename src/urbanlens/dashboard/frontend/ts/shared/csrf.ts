/**
 * CSRF token used for fetch() calls that bypass HTMX (which already injects
 * X-CSRFToken via its own htmx:configRequest listener in base.html).
 */
export function getCsrfToken(): string {
    return window.csrftoken ?? "";
}
