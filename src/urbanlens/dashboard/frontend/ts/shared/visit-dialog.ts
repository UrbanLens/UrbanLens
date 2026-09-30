/**
 * The Memories pages' shared visit dialog (``partials/memories/_visit_dialog.html``): ``data-visit-dialog-url`` on a
 * button, or ``openVisitDialog`` from script, loads that visit's form into it and opens it.
 */

export function openVisitDialog(url: string): void {
    const dialog = document.getElementById("memories-visit-dialog");
    const body = document.getElementById("memories-visit-dialog-body");
    if (!(dialog instanceof HTMLDialogElement) || !body || !window.htmx) return;
    const loading = document.createElement("p");
    loading.className = "visit-edit-loading";
    loading.textContent = "Loading…";
    body.replaceChildren(loading);
    void window.htmx.ajax("GET", url, { target: "#memories-visit-dialog-body", swap: "innerHTML" }).then(() => dialog.showModal());
}

let installed = false;

export function installVisitDialog(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", (event) => {
        const button = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-visit-dialog-url]") : null;
        if (button?.dataset.visitDialogUrl) openVisitDialog(button.dataset.visitDialogUrl);
    });
}
