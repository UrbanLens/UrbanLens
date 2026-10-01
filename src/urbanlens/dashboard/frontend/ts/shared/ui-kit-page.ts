/**
 * Site admin > UI components (``pages/site_admin_ui_components.html``): just enough behaviour for the demos to be
 * clicked through. The demo dialog opens and closes through ``data-dialog-open``/``data-dialog-close``.
 */

import { toast } from "./dialogs";

const TOASTS = {
    success: "Changes saved successfully.",
    error: "Something went wrong.",
    info: "Here is some helpful context.",
    warning: "Please review before continuing.",
} as const;

const isToastKind = (kind: string | undefined): kind is keyof typeof TOASTS => kind !== undefined && Object.hasOwn(TOASTS, kind);

/** Returns the uninstaller. */
export function installUiKitPage(root: Document): () => void {
    const onClick = (event: MouseEvent): void => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        const tab = target.closest<HTMLElement>("[data-ui-kit-tab]");
        if (tab) {
            for (const other of tab.closest(".ul-tab-bar")?.querySelectorAll(".ul-tab") ?? []) other.classList.remove("ul-tab--active", "active");
            tab.classList.add("ul-tab--active");
        }
        const dropdown = root.getElementById("ui-kit-dropdown");
        if (dropdown) {
            if (target.closest("#ui-kit-dropdown-toggle")) dropdown.classList.toggle("is-open");
            else if (!dropdown.contains(target)) dropdown.classList.remove("is-open");
        }
        const kind = target.closest<HTMLElement>("[data-toast]")?.dataset.toast;
        if (isToastKind(kind)) toast[kind](TOASTS[kind]);
        if (target.closest("a[data-demo-link]")) event.preventDefault();
    };
    const onChange = (event: Event): void => {
        const radio = event.target instanceof HTMLInputElement && event.target.type === "radio" ? event.target : null;
        const label = radio?.closest(".ul-radio-button");
        if (!radio || !label) return;
        for (const other of root.querySelectorAll(`.ul-radio-button input[name="${CSS.escape(radio.name)}"]`)) other.closest(".ul-radio-button")?.classList.toggle("active", other === radio);
    };
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
    return () => {
        root.removeEventListener("click", onClick);
        root.removeEventListener("change", onChange);
    };
}
