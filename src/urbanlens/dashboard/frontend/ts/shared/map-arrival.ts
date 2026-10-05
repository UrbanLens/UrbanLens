/**
 * What the map page does with the address it was opened at, once htmx has wired the page: ``?import=1`` (the import
 * help center links here) opens the import dialog, ``?suggestions_imported=1`` confirms the accepted suggestions
 * landed, and a new user with pending suggestions is offered them.
 */
import { toast } from "./dialogs";

export interface MapArrivalOptions {
    showPinSuggestionsIntro: boolean;
    /** Memories > Locations, where the suggestions are reviewed. */
    suggestionsUrl: string;
}

export function handleMapArrival(search: string, options: MapArrivalOptions): void {
    const params = new URLSearchParams(search);
    if (params.get("import") === "1") document.getElementById("import-pins-button")?.click();

    const intro = document.getElementById("pin-suggestions-intro-dialog");
    if (options.showPinSuggestionsIntro && intro instanceof HTMLDialogElement) intro.showModal();
    document.getElementById("pin-suggestions-intro-accept")?.addEventListener("click", () => {
        const target = new URL(options.suggestionsUrl, window.location.origin);
        if (target.origin !== window.location.origin) return;
        target.searchParams.set("onboarding", "1");
        window.location.assign(`${target.pathname}${target.search}`);
    });

    if (params.get("suggestions_imported") === "1") toast.success("Your accepted pin suggestions are now on the map.");
}
