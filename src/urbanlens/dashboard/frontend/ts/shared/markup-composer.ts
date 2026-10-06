/**
 * The map composer's drawing tools (`#comment-map-composer`, themes/base.html): the draw tools, the
 * selection and what can be changed about it, the Layers list, undo and redo, and turning the map.
 *
 * `static/js/comment-map.js` owns the dialog - opening it, the search, the title, saving and the
 * download - and hands this the map once it exists. What is drawn lives in a {@link MarkupDocument};
 * {@link createMarkupEditor} draws it onto the map and turns presses on it into edits.
 */
import { MarkupDocument, type DocItem, type ShapeType } from "./markup-document";
import { createMarkupEditor, type MarkupEditor } from "./markup-editor";
import { MarkupEngine, safeColor, textRotation, type LatLngTuple, type ShapeSpec } from "./markup-engine";
import { TEXT_SIZE_MAX, TEXT_SIZE_MIN } from "./markup-geometry";
import { canTurn, ensureLeafletRotate, rotateOptions, toCompassBearing, toContentTurn, type ScriptSource } from "./leaflet-rotate-loader";

// `L` is a page global from a CDN <script>; see markup-engine.ts.
declare const L: typeof import("leaflet");

/** Shapes with an inside, whose fill can be seen through. */
const FILLED: ReadonlySet<ShapeType> = new Set(["circle", "rect", "polygon"]);

const TYPE_ICONS: Record<ShapeType, string> = {
    line: "timeline",
    arrow: "arrow_forward",
    circle: "radio_button_unchecked",
    rect: "crop_square",
    polygon: "pentagon",
    text: "text_fields",
    pin: "location_on",
};

/** How far one press of a turn button turns the map. */
const TURN_STEP = 15;

/** Where the open state of the Layers list is remembered, per browser. */
const LAYERS_OPEN_KEY = "ul_cmc_layers_open";

export interface MarkupComposer {
    readonly document: MarkupDocument;
    readonly editor: MarkupEditor;
    /** Replaces the drawing with a saved map's shapes, starting history over. */
    load: (shapes: readonly unknown[] | null | undefined) => void;
    /** What is kept: visible shapes, bottom first, as the snapshot format. */
    shapes: () => ShapeSpec[];
    /** Which way is up, in degrees clockwise from north. 0 when the map cannot turn. */
    bearing: () => number;
    setBearing: (compassBearing: number) => void;
    /** Whether this map was built to turn. */
    canRotate: () => boolean;
    /** Puts the tools down and lets go of the selection - the dialog is closing. */
    deactivate: () => void;
    destroy: () => void;
}

/** Whether a key pressed here is typing, which no shortcut may take. */
function isTextEntry(target: EventTarget | null): boolean {
    if (!(target instanceof Element)) return false;
    if (target.closest("[contenteditable=''], [contenteditable='true']")) return true;
    if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
    if (!(target instanceof HTMLInputElement)) return false;
    return !["range", "color", "checkbox", "radio", "button", "submit", "reset", "file", "image"].includes(target.type);
}

function byId<T extends HTMLElement>(root: ParentNode, id: string): T | null {
    return root.querySelector<T>(`#${id}`);
}

function readLayersOpen(): boolean | null {
    try {
        const value = window.localStorage.getItem(LAYERS_OPEN_KEY);
        return value === null ? null : value === "1";
    } catch {
        return null;
    }
}

function writeLayersOpen(open: boolean): void {
    try {
        window.localStorage.setItem(LAYERS_OPEN_KEY, open ? "1" : "0");
    } catch {
        /* storage unavailable - the list just opens as it would by default */
    }
}

/**
 * Builds the map a composer draws on, turning if leaflet-rotate can be had.
 * @param el - The map's element.
 * @param rotateSource - Where leaflet-rotate is loaded from, or null to build a map that does not turn.
 */
export async function createComposerMap(el: HTMLElement, rotateSource: ScriptSource | null, options: L.MapOptions = {}): Promise<L.Map> {
    await ensureLeafletRotate(rotateSource);
    return L.map(el, { ...options, ...rotateOptions(0) } as L.MapOptions);
}

/**
 * Wires the composer's drawing tools to `map`.
 * @param map - The composer's map.
 * @param root - The dialog, which every control is looked up inside.
 */
export function createMarkupComposer(map: L.Map, root: HTMLElement): MarkupComposer {
    const doc = new MarkupDocument();
    const el = {
        undo: byId<HTMLButtonElement>(root, "cmc-undo"),
        redo: byId<HTMLButtonElement>(root, "cmc-redo"),
        clear: byId<HTMLButtonElement>(root, "cmc-clear"),
        locate: byId<HTMLButtonElement>(root, "cmc-pin-location"),
        color: byId<HTMLInputElement>(root, "cmc-color"),
        width: byId<HTMLInputElement>(root, "cmc-width"),
        widthLabel: byId<HTMLElement>(root, "cmc-width-label"),
        fillWrap: byId<HTMLElement>(root, "cmc-fill-wrap"),
        fill: byId<HTMLInputElement>(root, "cmc-fill"),
        text: byId<HTMLInputElement>(root, "cmc-text-label"),
        rotationWrap: byId<HTMLElement>(root, "cmc-rotation-wrap"),
        rotation: byId<HTMLInputElement>(root, "cmc-rotation"),
        rotationValue: byId<HTMLOutputElement>(root, "cmc-rotation-value"),
        target: byId<HTMLElement>(root, "cmc-style-target"),
        removePoint: byId<HTMLButtonElement>(root, "cmc-remove-point"),
        deleteSelected: byId<HTMLButtonElement>(root, "cmc-delete-selected"),
        deselect: byId<HTMLButtonElement>(root, "cmc-deselect"),
        hint: byId<HTMLElement>(root, "cmc-hint"),
        layersPanel: byId<HTMLDetailsElement>(root, "cmc-layers-panel"),
        layersCount: byId<HTMLElement>(root, "cmc-layers-count"),
        layersList: byId<HTMLElement>(root, "cmc-layers-list"),
        layersEmpty: byId<HTMLElement>(root, "cmc-layers-empty"),
        layersHiddenNote: byId<HTMLElement>(root, "cmc-layers-hidden-note"),
    };
    const toolButtons = [...root.querySelectorAll<HTMLButtonElement>(".cmc-tool-btn[data-tool]")];

    /** What a new shape is drawn with; the inputs show these whenever nothing is selected. */
    const defaults = { color: safeColor(el.color?.value, "#e74c3c"), width: Number(el.width?.value) || 3 };
    const widthRange = { min: el.width?.min || "1", max: el.width?.max || "12" };
    let drawHint = "";
    let notice = "";
    let noticeTimer: number | null = null;

    // -- Drawing -------------------------------------------------------------------------------

    const session = MarkupEngine.createDrawSession(map, {
        getColor: () => defaults.color,
        getWidth: () => defaults.width,
        getTextLabel: () => (el.text?.value ?? "").trim(),
        onCommit: (type, latlngs, extras) => {
            const shape: ShapeSpec = { type: type as ShapeType, latlngs: latlngs as LatLngTuple[], color: defaults.color, stroke_width: defaults.width };
            let needsLabel = false;
            if (type === "text") {
                const label = typeof extras.label === "string" ? extras.label.trim() : "";
                needsLabel = !label;
                shape.label = label || "Text";
                shape.stroke_width = 16;
            }
            // An outline as wide as the width says - without a border colour the renderer draws every outline at 2px.
            if (FILLED.has(shape.type)) shape.border_color = defaults.color;
            const id = doc.add(shape);
            if (id && needsLabel) {
                // Placed without words: put the tool down and open the new label for typing. After
                // the click that placed it has finished, or the same click lands on empty map and
                // lets go of the label it just made.
                window.setTimeout(() => {
                    session.deactivate();
                    doc.select(id);
                    el.text?.focus();
                    el.text?.select();
                }, 0);
            }
        },
        onHintChange: (text) => {
            drawHint = text;
            syncHint();
        },
        onToolChange: (tool) => {
            for (const button of toolButtons) {
                const active = button.dataset.tool === tool;
                button.classList.toggle("is-active", active);
                button.setAttribute("aria-pressed", String(active));
            }
            editor.setInteractive(!tool);
            map.getContainer().classList.toggle("is-drawing", !!tool);
            syncControls();
            if (tool === "text") window.setTimeout(() => el.text?.focus(), 0);
        },
    });

    const editor = createMarkupEditor(map, doc, {
        onChange: () => syncControls(),
        onNotice: (text) => showNotice(text),
    });

    for (const button of toolButtons) {
        button.setAttribute("aria-pressed", "false");
        button.addEventListener("click", () => {
            const tool = button.dataset.tool!;
            if (session.getCurrentTool() === tool) session.deactivate();
            else {
                doc.select(null);
                session.startTool(tool);
            }
        });
    }

    el.locate?.addEventListener("click", () => {
        if (!navigator.geolocation) {
            window.toastr?.warning("Geolocation is not available in this browser.");
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                doc.add({ type: "pin", latlngs: [[pos.coords.latitude, pos.coords.longitude]], color: defaults.color });
                map.panTo([pos.coords.latitude, pos.coords.longitude]);
            },
            () => window.toastr?.warning("Couldn't get your location. Check your browser's location permission."),
            { enableHighAccuracy: true, timeout: 10000 },
        );
    });

    el.undo?.addEventListener("click", () => doc.undo());
    el.redo?.addEventListener("click", () => doc.redo());
    el.clear?.addEventListener("click", () => {
        session.cancelShape();
        doc.clear();
    });

    // -- Style: the selection's, or what the next shape is drawn with ---------------------------

    function selected(): DocItem | undefined {
        return doc.get(doc.selectedId());
    }

    /** Applies an input's value to the selection as one undo step per gesture (`change` closes it). */
    function live(input: HTMLInputElement | null, apply: (item: DocItem, value: string) => void, setDefault?: (value: string) => void): void {
        if (!input) return;
        input.addEventListener("input", () => {
            const item = selected();
            if (item) {
                doc.begin();
                apply(item, input.value);
            } else {
                setDefault?.(input.value);
            }
        });
        input.addEventListener("change", () => doc.commit());
        // A gesture abandoned by leaving the field still closes its undo step.
        input.addEventListener("blur", () => doc.commit());
    }

    live(
        el.color,
        (item, value) => {
            const color = safeColor(value, item.shape.color);
            doc.update(item.id, (shape) => {
                // A border that matched the fill keeps matching it.
                if (shape.border_color && shape.border_color === shape.color) shape.border_color = color;
                shape.color = color;
            });
        },
        (value) => (defaults.color = safeColor(value, defaults.color)),
    );
    live(
        el.width,
        (item, value) => {
            const n = Number(value);
            if (!Number.isFinite(n)) return;
            doc.update(item.id, (shape) => {
                if (shape.type === "text") shape.stroke_width = Math.max(TEXT_SIZE_MIN, Math.min(TEXT_SIZE_MAX, Math.round(n)));
                else {
                    shape.stroke_width = Math.max(1, Math.min(50, Math.round(n)));
                    if (FILLED.has(shape.type) && !shape.border_color) shape.border_color = shape.color ?? defaults.color;
                }
            });
        },
        (value) => (defaults.width = Math.max(1, Math.min(50, Math.round(Number(value) || defaults.width)))),
    );
    live(el.fill, (item, value) => {
        const n = Number(value);
        if (Number.isFinite(n) && FILLED.has(item.shape.type)) doc.update(item.id, (shape) => void (shape.fill_opacity = Math.max(0, Math.min(100, Math.round(n)))));
    });
    live(el.text, (item, value) => {
        if (item.shape.type === "text") doc.update(item.id, (shape) => void (shape.label = value.slice(0, 500)));
    });
    live(el.rotation, (item, value) => {
        const n = Number(value);
        if (item.shape.type !== "text" || !Number.isFinite(n)) return;
        doc.update(item.id, (shape) => {
            const turn = textRotation({ rotation: n });
            if (turn) shape.rotation = turn;
            else delete shape.rotation;
        });
    });
    // Enter in the label field finishes the label rather than submitting anything.
    el.text?.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && selected()?.shape.type === "text") {
            event.preventDefault();
            doc.commit();
            el.text?.blur();
        }
    });

    el.removePoint?.addEventListener("click", () => editor.removeActiveVertex());
    el.deleteSelected?.addEventListener("click", () => editor.deleteSelection());
    el.deselect?.addEventListener("click", () => doc.select(null));

    // -- The Layers list -------------------------------------------------------------------------

    if (el.layersPanel) {
        const remembered = readLayersOpen();
        el.layersPanel.open = remembered ?? !window.matchMedia?.("(max-width: 720px)").matches;
        el.layersPanel.addEventListener("toggle", () => writeLayersOpen(el.layersPanel!.open));
    }

    function iconButton(action: string, icon: string, label: string, disabled = false): HTMLButtonElement {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `cmc-layer-action cmc-layer-action--${action}`;
        button.dataset.layerAction = action;
        button.setAttribute("aria-label", label);
        button.title = label;
        button.disabled = disabled;
        const i = document.createElement("i");
        i.className = "material-symbols-outlined";
        i.setAttribute("aria-hidden", "true");
        i.textContent = icon;
        button.appendChild(i);
        return button;
    }

    function renderLayers(): void {
        const list = el.layersList;
        if (!list) return;
        const items = doc.items();
        const selectedId = doc.selectedId();
        const rows: HTMLElement[] = [];
        // Topmost first, as a stack of layers reads.
        for (let index = items.length - 1; index >= 0; index--) {
            const item = items[index]!;
            const name = doc.label(item);
            const row = document.createElement("li");
            row.className = "cmc-layer-row";
            row.dataset.markupId = item.id;
            row.classList.toggle("is-selected", item.id === selectedId);
            row.classList.toggle("is-hidden", item.hidden);

            const pick = document.createElement("button");
            pick.type = "button";
            pick.className = "cmc-layer-name";
            pick.dataset.layerAction = "select";
            pick.setAttribute("aria-pressed", String(item.id === selectedId));
            pick.disabled = item.hidden;
            pick.title = item.hidden ? `${name} (hidden)` : `Select ${name}`;
            const icon = document.createElement("i");
            icon.className = "material-symbols-outlined";
            icon.setAttribute("aria-hidden", "true");
            icon.textContent = TYPE_ICONS[item.shape.type];
            icon.style.color = safeColor(item.shape.color, "#e74c3c");
            const text = document.createElement("span");
            text.className = "cmc-layer-label";
            text.textContent = name;
            pick.append(icon, text);

            row.append(
                pick,
                iconButton("visibility", item.hidden ? "visibility_off" : "visibility", item.hidden ? `Show ${name}` : `Hide ${name}`),
                iconButton("up", "arrow_upward", `Move ${name} up`, index === items.length - 1),
                iconButton("down", "arrow_downward", `Move ${name} down`, index === 0),
                iconButton("delete", "delete", `Delete ${name}`),
            );
            rows.push(row);
        }
        list.replaceChildren(...rows);
        const hidden = items.filter((item) => item.hidden).length;
        if (el.layersCount) el.layersCount.textContent = items.length ? String(items.length) : "";
        if (el.layersEmpty) el.layersEmpty.hidden = items.length > 0;
        if (el.layersHiddenNote) el.layersHiddenNote.hidden = hidden === 0;
        list.querySelector(".is-selected")?.scrollIntoView?.({ block: "nearest" });
    }

    el.layersList?.addEventListener("click", (event) => {
        const button = event.target instanceof Element ? event.target.closest<HTMLButtonElement>("button[data-layer-action]") : null;
        const id = button?.closest<HTMLElement>(".cmc-layer-row")?.dataset.markupId;
        if (!button || !id || button.disabled) return;
        session.deactivate();
        switch (button.dataset.layerAction) {
            case "select":
                doc.select(doc.selectedId() === id ? null : id);
                break;
            case "visibility":
                doc.setHidden(id, !doc.get(id)?.hidden);
                break;
            case "up":
                doc.move(id, 1);
                break;
            case "down":
                doc.move(id, -1);
                break;
            case "delete":
                doc.remove(id);
                break;
        }
        // The row was drawn afresh; keep the keyboard where it was.
        el.layersList?.querySelector<HTMLButtonElement>(`.cmc-layer-row[data-markup-id="${id}"] button[data-layer-action="${button.dataset.layerAction}"]`)?.focus();
    });

    // -- Keeping the controls in step ----------------------------------------------------------

    function show(node: HTMLElement | null, visible: boolean): void {
        if (node) node.hidden = !visible;
    }

    function setWidthRange(min: string, max: string, value: number): void {
        if (!el.width) return;
        el.width.min = min;
        el.width.max = max;
        el.width.value = String(value);
    }

    function syncHint(): void {
        if (!el.hint) return;
        let text = drawHint;
        if (!session.getCurrentTool()) {
            if (notice) text = notice;
            else if (selected()) {
                text = editor.activeVertex() !== null ? "Point picked - Delete removes it, or drag it." : "Drag to move. Handles reshape it. Delete removes it.";
            } else text = doc.hasMarkup() ? "Click a shape to change it." : "";
        }
        el.hint.textContent = text;
    }

    function showNotice(text: string): void {
        notice = text;
        if (noticeTimer !== null) window.clearTimeout(noticeTimer);
        noticeTimer = window.setTimeout(() => {
            notice = "";
            noticeTimer = null;
            syncHint();
        }, 3500);
        syncHint();
    }

    function syncControls(): void {
        if (el.undo) el.undo.disabled = !doc.canUndo();
        if (el.redo) el.redo.disabled = !doc.canRedo();
        if (el.clear) el.clear.disabled = !doc.hasMarkup();

        const item = selected();
        const tool = session.getCurrentTool();
        const shape = item?.shape;
        const focused = document.activeElement;
        if (el.target) el.target.textContent = item ? doc.label(item) : "New shapes";
        root.classList.toggle("has-markup-selection", !!item);

        // Values are only written while the field is not being typed or dragged in.
        if (el.color && focused !== el.color) el.color.value = safeColor(shape?.color ?? defaults.color, defaults.color);
        if (el.width && focused !== el.width) {
            if (shape?.type === "text") setWidthRange(String(TEXT_SIZE_MIN), String(TEXT_SIZE_MAX), shape.stroke_width ?? 16);
            else setWidthRange(widthRange.min, widthRange.max, shape?.stroke_width ?? defaults.width);
        }
        if (el.widthLabel) el.widthLabel.textContent = shape?.type === "text" ? "Size" : "Width";
        show(el.width?.closest<HTMLElement>(".cmc-width-wrap") ?? null, shape?.type !== "pin");

        const filled = !!shape && FILLED.has(shape.type);
        show(el.fillWrap, filled);
        if (filled && el.fill && focused !== el.fill) el.fill.value = String(shape!.fill_opacity ?? 87);

        const isText = shape?.type === "text";
        show(el.text, tool === "text" || isText);
        if (isText && el.text && focused !== el.text) el.text.value = shape!.label ?? "";
        if (!isText && tool !== "text" && el.text && el.text.dataset.forSelection === "1") el.text.value = "";
        if (el.text) el.text.dataset.forSelection = isText ? "1" : "";
        show(el.rotationWrap, isText);
        if (isText && el.rotation && focused !== el.rotation) el.rotation.value = String(Math.round(textRotation(shape!)));
        if (el.rotationValue) el.rotationValue.textContent = isText ? `${Math.round(textRotation(shape!))}°` : "";

        show(el.removePoint, !!item && editor.canRemoveActiveVertex());
        show(el.deleteSelected, !!item);
        show(el.deselect, !!item);
        renderLayers();
        syncHint();
    }

    // -- Keyboard ------------------------------------------------------------------------------

    let escapeHandledAt = 0;

    function isOpen(): boolean {
        return root instanceof HTMLDialogElement ? root.open : root.isConnected;
    }

    function onKeyDown(event: KeyboardEvent): void {
        if (!isOpen() || event.defaultPrevented) return;
        const target = event.target;
        // Keys typed somewhere else on the page are not this dialog's.
        if (target instanceof Node && target !== document.body && target !== document.documentElement && !root.contains(target)) return;
        // While a tool is armed the draw session answers Escape and Enter itself.
        if (session.getCurrentTool()) return;
        const typing = isTextEntry(target);
        const key = event.key;
        const mod = event.ctrlKey || event.metaKey;
        let handled = false;

        if ((key === "Delete" || key === "Backspace") && !typing && !mod) {
            handled = editor.deleteSelection();
        } else if (key === "Escape" && !typing) {
            handled = editor.escape();
            if (handled) escapeHandledAt = Date.now();
        } else if (mod && !typing && (key === "z" || key === "Z")) {
            handled = event.shiftKey ? doc.redo() : doc.undo();
        } else if (mod && !typing && (key === "y" || key === "Y")) {
            handled = doc.redo();
        } else if (key.startsWith("Arrow") && !typing && !(target instanceof HTMLInputElement && target.type === "range") && doc.selectedId()) {
            const step = event.shiftKey ? 10 : 1;
            const dx = key === "ArrowLeft" ? -step : key === "ArrowRight" ? step : 0;
            const dy = key === "ArrowUp" ? -step : key === "ArrowDown" ? step : 0;
            handled = editor.nudge(dx, dy);
        }
        if (!handled) return;
        event.preventDefault();
        event.stopPropagation();
    }
    document.addEventListener("keydown", onKeyDown, true);

    // Escape that let go of something must not also close the dialog.
    function onCancel(event: Event): void {
        if (session.getCurrentTool() || editor.isDragging() || Date.now() - escapeHandledAt < 500) event.preventDefault();
    }
    root.addEventListener("cancel", onCancel);

    // -- Turning the map ------------------------------------------------------------------------

    let rotateControl: L.Control | null = null;
    let compass: HTMLElement | null = null;

    function bearing(): number {
        return canTurn(map) ? toCompassBearing(map.getBearing()) : 0;
    }

    function syncCompass(): void {
        if (!compass || !canTurn(map)) return;
        const up = Math.round(bearing()) % 360;
        const needle = compass.querySelector<HTMLElement>("i");
        // The needle points at north, wherever on screen that now is.
        if (needle) needle.style.transform = `rotate(${map.getBearing()}deg)`;
        const label = up ? `Reset to north up (now turned ${up}°)` : "North is up";
        compass.setAttribute("aria-label", label);
        compass.title = label;
        compass.classList.toggle("is-turned", up !== 0);
    }

    if (canTurn(map)) {
        const RotateControl = L.Control.extend({
            options: { position: "topleft" },
            onAdd(): HTMLElement {
                const bar = L.DomUtil.create("div", "leaflet-bar cmc-rotate-control");
                const make = (icon: string, label: string, onClick: () => void): HTMLButtonElement => {
                    const button = L.DomUtil.create("button", "cmc-rotate-btn", bar) as HTMLButtonElement;
                    button.type = "button";
                    button.setAttribute("aria-label", label);
                    button.title = label;
                    const i = document.createElement("i");
                    i.className = "material-symbols-outlined";
                    i.setAttribute("aria-hidden", "true");
                    i.textContent = icon;
                    button.appendChild(i);
                    button.addEventListener("click", (event) => {
                        event.preventDefault();
                        onClick();
                    });
                    return button;
                };
                make("rotate_left", "Turn the map anticlockwise", () => canTurn(map) && map.setBearing(map.getBearing() - TURN_STEP));
                compass = make("navigation", "North is up", () => canTurn(map) && map.setBearing(0));
                compass.classList.add("cmc-rotate-compass");
                make("rotate_right", "Turn the map clockwise", () => canTurn(map) && map.setBearing(map.getBearing() + TURN_STEP));
                L.DomEvent.disableClickPropagation(bar);
                L.DomEvent.disableScrollPropagation(bar);
                return bar;
            },
        });
        rotateControl = new RotateControl();
        rotateControl.addTo(map);
        map.on("rotate", syncCompass);
        syncCompass();
    }

    syncControls();

    return {
        document: doc,
        editor,
        load(shapes) {
            session.deactivate();
            doc.load(shapes);
        },
        shapes: () => doc.visibleShapes(),
        bearing,
        setBearing(compassBearing) {
            if (canTurn(map)) map.setBearing(toContentTurn(compassBearing));
            syncCompass();
        },
        canRotate: () => canTurn(map),
        deactivate() {
            session.deactivate();
            doc.commit();
            doc.select(null);
        },
        destroy() {
            document.removeEventListener("keydown", onKeyDown, true);
            root.removeEventListener("cancel", onCancel);
            map.off("rotate", syncCompass);
            rotateControl?.remove();
            if (noticeTimer !== null) window.clearTimeout(noticeTimer);
            editor.destroy();
            session.destroy();
        },
    };
}

export const MarkupComposerFactory = { create: createMarkupComposer, createMap: createComposerMap, ensureRotation: ensureLeafletRotate, rotateOptions };

export function installGlobalMarkupComposer(): void {
    window.MarkupComposer = MarkupComposerFactory;
}

declare global {
    interface Window {
        MarkupComposer: typeof MarkupComposerFactory;
    }
}
