/**
 * The satellite and street-view fragments (``pages/location/satellite_view.html`` and ``street_view.html``) arrive by
 * htmx swap and name their controls in markup: ``data-carousel-action`` on the buttons, ``data-carousel-img`` on each
 * image whose failure drops its slide. The page that owns the carousels says what each does.
 *
 * Images are watched from ``document`` in the capture phase, set up before any fragment can arrive, so one that
 * fails the moment it is inserted is still caught.
 */
import { delegateActions } from "./delegated-actions";

interface Carousel {
    prev(): void;
    next(): void;
    /** Shows the first slide once the fragment is in the page. */
    start(): void;
    drop(img: HTMLImageElement): void;
}

export interface CarouselControls {
    satellite: Carousel;
    street: Carousel & { showStatic(button: HTMLButtonElement): void };
}

const CAROUSELS = { satellite: "#sat-carousel", street: "#sv-carousel" } as const;

export function installCarouselControls(controls: CarouselControls): void {
    delegateActions(document, "carousel-action", {
        "satellite-prev": () => controls.satellite.prev(),
        "satellite-next": () => controls.satellite.next(),
        "street-prev": () => controls.street.prev(),
        "street-next": () => controls.street.next(),
        "street-static": (button) => {
            if (button instanceof HTMLButtonElement) controls.street.showStatic(button);
        },
    });

    document.addEventListener(
        "error",
        (event) => {
            const img = event.target;
            if (!(img instanceof HTMLImageElement) || !img.hasAttribute("data-carousel-img")) return;
            if (img.closest(CAROUSELS.satellite)) controls.satellite.drop(img);
            else if (img.closest(CAROUSELS.street)) controls.street.drop(img);
        },
        true,
    );

    document.addEventListener("htmx:load", (event) => {
        const root = event.target;
        if (!(root instanceof Element)) return;
        const brings = (selector: string) => root.matches(selector) || root.querySelector(selector) !== null;
        if (brings(CAROUSELS.satellite)) controls.satellite.start();
        if (brings(CAROUSELS.street)) controls.street.start();
    });
}
