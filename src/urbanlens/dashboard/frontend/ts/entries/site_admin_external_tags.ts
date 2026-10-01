import { initExternalTagMapping } from "../shared/external-tag-mapping";

function init(): void {
    initExternalTagMapping();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
