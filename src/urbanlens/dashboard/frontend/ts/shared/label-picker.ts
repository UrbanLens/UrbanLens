/**
 * Shared label pickers - the one implementation behind every place labels are
 * picked (docs/PROBLEMS.md "Saved-filter include/exclude label picker" entry;
 * extraction authorized 2026-07-23).
 *
 * Two factories, installed globally as `window.UrbanLensLabelPicker` via the
 * core.js classic bundle (see entries-classic/core.ts):
 *
 * - `createFilterPicker(options)` - the rich include/exclude picker extracted
 *   from the main map's filter sidebar: click-to-include /
 *   right-click-to-exclude, removable chips, drag between the Include and
 *   Exclude columns (or out to remove), an AND/OR combinator toggle, and a
 *   formula bar (`(Visited / "Want To Go") - Demolished`) whose parsed groups
 *   serialize to the `label_groups` JSON shape `PinQuerySet.apply_label_groups`
 *   consumes. Consumers: the main map sidebar and the saved-filter
 *   dialog/detail page.
 * - `createChipPicker(options)` - the flat search-and-chips picker (previously
 *   duplicated as the map page's `_makeLabelChipPicker` and the saved-filter
 *   scripts' `_sfMakeLabelPicker`). Consumers: the bulk-edit dialog's
 *   add/remove label sections (two independent candidate pools, so the rich
 *   include/exclude pairing deliberately does not apply there).
 *
 * The DOM contract is class-based (`.fp-label-avail` buttons carrying
 * `data-label-id/-name/-text/-color/-icon`, chip/columns markup styled by
 * `_map.scss`'s `fp-*` rules, which are global) with every element handed in
 * explicitly - no hardcoded ids, so several pickers can coexist on one page.
 */

/** One selectable label, as read off an availability button's data attributes. */
export interface LabelEntry {
    id: string;
    label: string;
    color: string;
    icon: string;
}

/** One serialized filter group - the `label_groups` JSON element shape. */
export interface LabelGroup {
    op: "and" | "or" | "not";
    ids: number[];
}

type ChipMode = "incl" | "excl";

function escHtml(value: unknown): string {
    return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Rich include/exclude filter picker
// ---------------------------------------------------------------------------

export interface FilterPickerElements {
    /** Container of the available-label buttons (`.fp-label-avail`). */
    list: HTMLElement;
    /** Wrapper around both selected-chip columns; hidden while empty. */
    selected: HTMLElement;
    colIncl: HTMLElement;
    colExcl: HTMLElement;
    inclChips: HTMLElement;
    exclChips: HTMLElement;
    /** AND/OR combinator button; shown only with 2+ included labels. */
    modeBtn?: HTMLElement | null;
    /** Hidden input receiving the serialized `label_groups` JSON. */
    groupsInput?: HTMLInputElement | null;
    formulaBar?: HTMLInputElement | null;
    formulaSuggestions?: HTMLElement | null;
    formulaErrors?: HTMLElement | null;
    /** Read-only rendering of a formula too complex for the 2-column UI. */
    formulaDisplay?: HTMLElement | null;
    formulaDisplayText?: HTMLElement | null;
    formulaDisplayClear?: HTMLElement | null;
    /** Optional accordion/section element toggled `fp-acc-active` while non-empty. */
    accordion?: HTMLElement | null;
}

export interface FilterPickerOptions {
    els: FilterPickerElements;
    /** Fired after every user-visible state change (the map triggers its filter form here). */
    onChange?: () => void;
    /**
     * Fired whenever state serializes, with the flat include/exclude id lists -
     * the saved-filter form mirrors these into hidden `tags`/`exclude_tags`
     * checkbox inputs so the legacy flat fields stay populated alongside
     * `label_groups`.
     */
    onSerialize?: (inclIds: string[], exclIds: string[], groups: LabelGroup[]) => void;
}

export interface FilterPickerApi {
    /** Remove every selection (and formula), restore the full list, fire onChange. */
    clear(): void;
    /** Re-render chips/columns/serialization from current state. */
    rebuild(): void;
    /** Replace state from parsed `label_groups` (formula-canonical when complex). */
    applyGroups(groups: LabelGroup[]): void;
    /** Union ids into the included set without touching excludes (saved-filter merge semantics). */
    mergeIncludeIds(ids: Array<number | string>): void;
    /** Whether nothing is selected and no formula is active. */
    isEmpty(): boolean;
}

export function createFilterPicker(options: FilterPickerOptions): FilterPickerApi {
    const els = options.els;
    const onChange = options.onChange || (() => {});

    // labelId(string) → 'incl' | 'excl'
    const labelState = new Map<string, ChipMode>();
    // How included labels combine in simple (non-formula) mode.
    let inclMode: "and" | "or" = "and";
    let dragId: string | null = null;
    let dropHandled = false; // set by drop handlers so dragend knows not to auto-remove
    let chipJustDragged = false; // suppresses the click browsers fire after dragend
    let availJustDragged = false; // same, for labels dragged straight from the list
    // Non-null in formula mode: parsed groups are the canonical state.
    let formulaGroups: LabelGroup[] | null = null;

    // -- Helpers -------------------------------------------------------------
    const availButtons = (): HTMLElement[] => Array.from(els.list.querySelectorAll<HTMLElement>(".fp-label-avail"));
    const availById = (id: string): HTMLElement | null => els.list.querySelector<HTMLElement>(`[data-label-id="${id}"]`);
    const labelTextForId = (id: string): string => availById(id)?.dataset.labelText || String(id);

    function quoteLabelName(label: string): string {
        return /[\s\/\-\+\(\)]/.test(label) ? `"${label}"` : label;
    }

    /** name(lowercase) → entry lookup built from the availability-list DOM. */
    function labelByNameMap(): Map<string, LabelEntry> {
        const m = new Map<string, LabelEntry>();
        availButtons().forEach((btn) => {
            m.set((btn.dataset.labelText || "").toLowerCase(), {
                id: btn.dataset.labelId || "",
                label: btn.dataset.labelText || "",
                color: btn.dataset.labelColor || "",
                icon: btn.dataset.labelIcon || "",
            });
        });
        return m;
    }

    // -- Chip HTML ------------------------------------------------------------
    function chipHtml(id: string, label: string, color: string, icon: string, mode: ChipMode): string {
        const bg = color ? color + "33" : mode === "incl" ? "rgba(34,197,94,.18)" : "rgba(239,68,68,.18)";
        const border = color ? color + "66" : mode === "incl" ? "rgba(34,197,94,.4)" : "rgba(239,68,68,.4)";
        const txtCol = mode === "incl" ? "#86efac" : "#fca5a5";
        const iconHtml = icon ? `<span style="font-size:.85em">${escHtml(icon)}</span>` : "";
        return `<span class="fp-label-chip fp-label-chip--${mode}" data-id="${escHtml(id)}" draggable="true"
                      title="Click to remove · Right-click to ${mode === "incl" ? "exclude" : "include"} · Drag to move or drop outside to remove"
                      style="background:${bg};border-color:${border};color:${txtCol}">
                    ${iconHtml}<span class="fp-label-chip-text">${escHtml(label)}</span>
                </span>`;
    }

    // -- Serialise state → label_groups JSON ----------------------------------
    // In formula mode the stored groups are used directly so structure (mixed
    // AND/OR) is preserved; otherwise groups derive from labelState + inclMode.
    function currentGroups(): LabelGroup[] {
        if (formulaGroups !== null) return formulaGroups;
        const inclIds = [...labelState.entries()].filter(([, m]) => m === "incl").map(([id]) => Number(id));
        const exclIds = [...labelState.entries()].filter(([, m]) => m === "excl").map(([id]) => Number(id));
        const groups: LabelGroup[] = [];
        if (inclIds.length > 0) groups.push({ op: inclMode, ids: inclIds });
        if (exclIds.length > 0) groups.push({ op: "not", ids: exclIds });
        return groups;
    }

    function serialize(): void {
        const groups = currentGroups();
        if (els.groupsInput) els.groupsInput.value = groups.length ? JSON.stringify(groups) : "";
        if (options.onSerialize) {
            const inclIds = [...labelState.entries()].filter(([, m]) => m === "incl").map(([id]) => id);
            const exclIds = [...labelState.entries()].filter(([, m]) => m === "excl").map(([id]) => id);
            options.onSerialize(inclIds, exclIds, groups);
        }
    }

    /** Formula text reconstruction shared by the bar echo and the complex display. */
    function groupsToFormulaParts(groups: LabelGroup[]): string[] {
        const byId = new Map([...labelByNameMap().values()].map((v) => [v.id, v]));
        const parts: string[] = [];
        for (const g of groups) {
            const names = g.ids.map((id) => {
                const entry = byId.get(String(id));
                return entry ? quoteLabelName(entry.label) : String(id);
            });
            if (g.op === "not") {
                names.forEach((n) => parts.push(`-${n}`));
            } else if (g.op === "or") {
                parts.push(names.length > 1 ? `(${names.join(" / ")})` : names[0] || "");
            } else {
                parts.push(...names);
            }
        }
        return parts;
    }

    function updateFormulaFromState(): void {
        const bar = els.formulaBar;
        if (!bar || document.activeElement === bar) return;
        if (formulaGroups !== null) {
            bar.value = groupsToFormulaParts(formulaGroups).join(" ");
        } else {
            const inclIds = [...labelState.entries()].filter(([, m]) => m === "incl").map(([id]) => id);
            const exclIds = [...labelState.entries()].filter(([, m]) => m === "excl").map(([id]) => id);
            const parts: string[] = [];
            if (inclIds.length > 0) {
                const names = inclIds.map((id) => quoteLabelName(labelTextForId(id)));
                parts.push(inclMode === "or" && names.length > 1 ? `(${names.join(" / ")})` : names.join(" "));
            }
            exclIds.forEach((id) => parts.push(`-${quoteLabelName(labelTextForId(id))}`));
            bar.value = parts.join(" ");
        }
    }

    // True when groups fit the simple 2-column UI: include groups all share one
    // op (all-AND, or a single OR group), plus any number of NOT groups.
    function isSimpleGroups(groups: LabelGroup[] | null): boolean {
        if (!groups) return true;
        const inclGroups = groups.filter((g) => g.op !== "not");
        if (inclGroups.length === 0) return true;
        const hasOr = inclGroups.some((g) => g.op === "or");
        const hasAnd = inclGroups.some((g) => g.op === "and");
        if (hasOr && hasAnd) return false;
        if (hasOr && inclGroups.length > 1) return false;
        return true;
    }

    // -- Rebuild selected chips UI --------------------------------------------
    function rebuild(): void {
        const isComplex = formulaGroups !== null && !isSimpleGroups(formulaGroups);

        if (isComplex) {
            // Complex formula: show the read-only text display instead of chips.
            els.selected.style.display = "none";
            if (els.formulaDisplay) els.formulaDisplay.style.display = "";
            if (els.formulaDisplayText) els.formulaDisplayText.textContent = groupsToFormulaParts(formulaGroups!).join(" + ");
            els.accordion?.classList.add("fp-acc-active");
            serialize();
            updateFormulaFromState();
            return;
        }

        if (els.formulaDisplay) els.formulaDisplay.style.display = "none";

        els.inclChips.innerHTML = "";
        els.exclChips.innerHTML = "";

        let hasIncl = false;
        let hasExcl = false;
        let inclCount = 0;
        labelState.forEach((mode, id) => {
            const avail = availById(id);
            const label = avail?.dataset.labelText || id;
            const color = avail?.dataset.labelColor || "";
            const icon = avail?.dataset.labelIcon || "";
            if (mode === "incl") {
                hasIncl = true;
                inclCount++;
                els.inclChips.insertAdjacentHTML("beforeend", chipHtml(id, label, color, icon, "incl"));
            } else {
                hasExcl = true;
                els.exclChips.insertAdjacentHTML("beforeend", chipHtml(id, label, color, icon, "excl"));
            }
        });

        [els.inclChips, els.exclChips].forEach((container) => {
            container.querySelectorAll<HTMLElement>(".fp-label-chip").forEach((chip) => {
                const id = chip.dataset.id || "";
                chip.addEventListener("click", () => {
                    if (!chipJustDragged) removeLabel(id);
                });
                chip.addEventListener("contextmenu", (e) => {
                    e.preventDefault();
                    toggleLabelMode(id);
                });
                chip.addEventListener("dragstart", (e) => {
                    dragId = id;
                    dropHandled = false;
                    chipJustDragged = true;
                    if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
                    chip.classList.add("fp-drag-active");
                    // Keep both columns visible so the user can drag between them.
                    els.colIncl.style.removeProperty("display");
                    els.colExcl.style.removeProperty("display");
                });
                chip.addEventListener("dragend", () => {
                    const draggedId = dragId;
                    dragId = null;
                    chip.classList.remove("fp-drag-active");
                    document.querySelectorAll(".fp-drag-over").forEach((el) => el.classList.remove("fp-drag-over"));
                    // No drop handler claimed the drag → dropped outside → remove.
                    if (draggedId && !dropHandled) removeLabel(draggedId);
                    dropHandled = false;
                    setTimeout(() => {
                        chipJustDragged = false;
                    }, 0);
                });
            });
        });

        if (els.modeBtn) {
            els.modeBtn.textContent = inclMode === "or" ? "OR" : "AND";
            els.modeBtn.classList.toggle("fp-label-mode-btn--or", inclMode === "or");
            els.modeBtn.style.display = inclCount > 1 ? "" : "none";
        }

        const anySelected = hasIncl || hasExcl;
        els.selected.style.display = anySelected ? "" : "none";
        els.colIncl.style.display = hasIncl ? "" : "none";
        els.colExcl.style.display = hasExcl ? "" : "none";
        els.accordion?.classList.toggle("fp-acc-active", anySelected);

        serialize();
        updateFormulaFromState();
    }

    // -- State mutations ------------------------------------------------------
    function addLabel(btn: HTMLElement, mode: ChipMode): void {
        formulaGroups = null; // chip interactions exit formula mode
        const id = btn.dataset.labelId || "";
        if (labelState.has(id)) {
            if (labelState.get(id) !== mode) toggleLabelMode(id);
            return;
        }
        labelState.set(id, mode);
        btn.style.display = "none";
        rebuild();
        onChange();
    }

    function toggleLabelMode(id: string): void {
        formulaGroups = null;
        if (!labelState.has(id)) return;
        labelState.set(id, labelState.get(id) === "incl" ? "excl" : "incl");
        rebuild();
        onChange();
    }

    function removeLabel(id: string): void {
        formulaGroups = null;
        labelState.delete(id);
        const btn = availById(id);
        if (btn) btn.style.display = "";
        rebuild();
        onChange();
    }

    function clear(): void {
        labelState.clear();
        formulaGroups = null;
        availButtons().forEach((btn) => {
            btn.style.display = "";
        });
        inclMode = "and";
        rebuild();
        onChange();
    }

    function toggleInclMode(): void {
        formulaGroups = null;
        inclMode = inclMode === "and" ? "or" : "and";
        rebuild();
        onChange();
    }

    // -- Column drop handlers (switch mode / add from list) -------------------
    function onColDrop(e: DragEvent, targetMode: ChipMode): void {
        (e.currentTarget as HTMLElement).classList.remove("fp-drag-over");
        if (!dragId) return;
        dropHandled = true;
        if (labelState.has(dragId)) {
            if (labelState.get(dragId) !== targetMode) toggleLabelMode(dragId);
        } else {
            const btn = availById(dragId);
            if (btn) addLabel(btn, targetMode);
        }
    }

    ([
        [els.colIncl, "incl"],
        [els.colExcl, "excl"],
    ] as Array<[HTMLElement, ChipMode]>).forEach(([col, mode]) => {
        col.addEventListener("dragover", (e) => {
            e.preventDefault();
            col.classList.add("fp-drag-over");
        });
        col.addEventListener("dragleave", () => col.classList.remove("fp-drag-over"));
        col.addEventListener("drop", (e) => onColDrop(e, mode));
    });

    els.modeBtn?.addEventListener("click", toggleInclMode);
    els.formulaDisplayClear?.addEventListener("click", clear);

    // Availability-list interactions are delegated so buttons appended later
    // (a label created inline from another dialog) work without re-wiring.
    els.list.addEventListener("click", (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>(".fp-label-avail");
        if (!btn) return;
        // Suppress the click some browsers fire on the source right after a drop.
        if (availJustDragged) return;
        addLabel(btn, "incl");
    });
    els.list.addEventListener("contextmenu", (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>(".fp-label-avail");
        if (!btn) return;
        e.preventDefault();
        addLabel(btn, "excl");
    });
    els.list.addEventListener("dragstart", (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>(".fp-label-avail");
        if (!btn) return;
        dragId = btn.dataset.labelId || "";
        dropHandled = false;
        availJustDragged = true;
        if ((e as DragEvent).dataTransfer) (e as DragEvent).dataTransfer!.effectAllowed = "move";
        btn.classList.add("fp-drag-active");
        // Reveal both columns as drop targets even with nothing selected yet.
        els.selected.style.display = "";
        els.colIncl.style.removeProperty("display");
        els.colExcl.style.removeProperty("display");
    });
    els.list.addEventListener("dragend", (e) => {
        const btn = (e.target as HTMLElement).closest<HTMLElement>(".fp-label-avail");
        if (!btn) return;
        dragId = null;
        btn.classList.remove("fp-drag-active");
        document.querySelectorAll(".fp-drag-over").forEach((el) => el.classList.remove("fp-drag-over"));
        // Not dropped into a column → restore the selected area's visibility.
        if (!dropHandled) rebuild();
        dropHandled = false;
        setTimeout(() => {
            availJustDragged = false;
        }, 0);
    });

    // -- Formula bar ----------------------------------------------------------
    interface FormulaToken {
        type: "LABEL" | "NOT" | "OR" | "LPAREN" | "RPAREN" | "UNKNOWN";
        text: string;
        id?: string;
        start: number;
        end: number;
    }

    (function initFormulaBar() {
        const barMaybe = els.formulaBar;
        const sugg = els.formulaSuggestions;
        const errs = els.formulaErrors;
        if (!barMaybe) return;
        // Explicitly re-typed: narrowing doesn't flow into the hoisted
        // function declarations below.
        const bar: HTMLInputElement = barMaybe;

        let activeSuggIdx = -1;

        // True when no label name starts with `partial` - typing more cannot fix it.
        function isDeadEnd(partial: string): boolean {
            if (!partial) return false;
            const lo = partial.toLowerCase();
            for (const name of labelByNameMap().keys()) {
                if (name.startsWith(lo)) return false;
            }
            return true;
        }

        function tokenize(text: string): FormulaToken[] {
            const byName = labelByNameMap();
            const sorted = [...byName.keys()].sort((a, b) => b.length - a.length);
            const tokens: FormulaToken[] = [];
            let i = 0;
            while (i < text.length) {
                const ch = text.charAt(i);
                if (ch === " " || ch === "+") {
                    i++;
                    continue;
                }
                if (ch === "-") {
                    tokens.push({ type: "NOT", text: "-", start: i, end: i + 1 });
                    i++;
                    continue;
                }
                if (ch === "/") {
                    tokens.push({ type: "OR", text: "/", start: i, end: i + 1 });
                    i++;
                    continue;
                }
                if (ch === "(") {
                    tokens.push({ type: "LPAREN", text: "(", start: i, end: i + 1 });
                    i++;
                    continue;
                }
                if (ch === ")") {
                    tokens.push({ type: "RPAREN", text: ")", start: i, end: i + 1 });
                    i++;
                    continue;
                }
                if (ch === '"') {
                    let j = i + 1;
                    while (j < text.length && text.charAt(j) !== '"') j++;
                    const raw = text.slice(i + 1, j);
                    const match = byName.get(raw.toLowerCase());
                    tokens.push(match ? { type: "LABEL", text: raw, id: match.id, start: i, end: j + 1 } : { type: "UNKNOWN", text: raw, start: i, end: j + 1 });
                    i = j + 1;
                    continue;
                }
                let matched = false;
                for (const name of sorted) {
                    if (text.slice(i).toLowerCase().startsWith(name)) {
                        const end = i + name.length;
                        const next = text.charAt(end);
                        if (!next || ' /+-()"'.includes(next)) {
                            const entry = byName.get(name)!;
                            tokens.push({ type: "LABEL", text: text.slice(i, end), id: entry.id, start: i, end });
                            i = end;
                            matched = true;
                            break;
                        }
                    }
                }
                if (!matched) {
                    let j = i;
                    while (j < text.length && !' /+-()"\t'.includes(text.charAt(j))) j++;
                    tokens.push({ type: "UNKNOWN", text: text.slice(i, j), start: i, end: j });
                    i = j;
                }
            }
            return tokens;
        }

        // (A / B) C → [{op:'or',ids:[A,B]}, {op:'and',ids:[C]}]; -A → [{op:'not',ids:[A]}]
        function parseTokens(tokens: FormulaToken[]): { groups: LabelGroup[]; errors: string[] } {
            const groups: LabelGroup[] = [];
            const errors: string[] = [];
            let i = 0;
            let negate = false;

            while (i < tokens.length) {
                const tok = tokens[i];
                if (!tok) break;
                if (tok.type === "NOT") {
                    negate = true;
                    i++;
                    continue;
                }
                if (tok.type === "OR") {
                    i++;
                    continue;
                }
                if (tok.type === "LPAREN") {
                    const orIds: string[] = [];
                    i++;
                    while (i < tokens.length) {
                        const inner = tokens[i];
                        if (!inner || inner.type === "RPAREN") break;
                        if (inner.type === "LABEL") orIds.push(inner.id!);
                        else if (inner.type === "UNKNOWN") errors.push(inner.text);
                        i++;
                    }
                    if (i < tokens.length) i++; // consume RPAREN
                    if (orIds.length) groups.push({ op: negate ? "not" : "or", ids: orIds.map(Number) });
                    negate = false;
                    continue;
                }
                if (tok.type === "LABEL") {
                    if (negate) {
                        // NOT applies to the single label only, no lookahead for OR.
                        groups.push({ op: "not", ids: [Number(tok.id)] });
                        negate = false;
                        i++;
                        continue;
                    }
                    // Consecutive OR-connected labels at top level: A / B / C → OR group.
                    const orIds: string[] = [tok.id!];
                    let j = i + 1;
                    while (j < tokens.length && tokens[j]?.type === "OR") {
                        j++;
                        const labelTok = tokens[j];
                        if (labelTok && labelTok.type === "LABEL") {
                            orIds.push(labelTok.id!);
                            j++;
                        }
                    }
                    if (orIds.length > 1) {
                        groups.push({ op: "or", ids: orIds.map(Number) });
                    } else {
                        groups.push({ op: "and", ids: [Number(tok.id)] });
                    }
                    negate = false;
                    i = j;
                    continue;
                }
                if (tok.type === "UNKNOWN") {
                    errors.push(tok.text);
                    negate = false;
                    i++;
                    continue;
                }
                i++;
            }
            return { groups, errors };
        }

        function currentToken(text: string, cursor: number): { text: string; start: number; end: number } {
            let start = cursor;
            while (start > 0 && !' /+-()"\t'.includes(text.charAt(start - 1))) start--;
            return { text: text.slice(start, cursor), start, end: cursor };
        }

        function showSuggestions(q: string): void {
            if (!sugg) return;
            q = q.toLowerCase().trim();
            activeSuggIdx = -1;
            if (!q) {
                sugg.hidden = true;
                return;
            }
            const matches = availButtons().filter((btn) => (btn.dataset.labelName || "").includes(q));
            if (!matches.length) {
                sugg.hidden = true;
                return;
            }
            sugg.innerHTML = matches
                .slice(0, 6)
                .map(
                    (btn, i) =>
                        `<div class="fp-formula-sugg" data-idx="${i}" data-id="${escHtml(btn.dataset.labelId || "")}" data-label="${escHtml(btn.dataset.labelText || "")}">
                            ${btn.dataset.labelIcon ? `<span>${escHtml(btn.dataset.labelIcon)}</span>` : ""}
                            ${escHtml(btn.dataset.labelText || "")}
                        </div>`,
                )
                .join("");
            sugg.hidden = false;
            sugg.querySelectorAll<HTMLElement>(".fp-formula-sugg").forEach((el) => {
                el.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    insertSuggestion(el.dataset.label || "");
                });
            });
        }

        function selectSuggestion(delta: number): boolean {
            if (!sugg || sugg.hidden) return false;
            const items = sugg.querySelectorAll(".fp-formula-sugg");
            if (!items.length) return false;
            activeSuggIdx = Math.max(0, Math.min(items.length - 1, activeSuggIdx + delta));
            items.forEach((el, i) => el.classList.toggle("active", i === activeSuggIdx));
            return true;
        }

        function insertSuggestion(label: string): void {
            const cursor = bar.selectionStart ?? bar.value.length;
            const tok = currentToken(bar.value, cursor);
            const quoted = quoteLabelName(label);
            bar.value = bar.value.slice(0, tok.start) + quoted + bar.value.slice(tok.end);
            const newPos = tok.start + quoted.length;
            bar.setSelectionRange(newPos, newPos);
            if (sugg) sugg.hidden = true;
            activeSuggIdx = -1;
            onFormulaChange();
        }

        // Per keystroke: suggestions + list filtering + dead-end errors only.
        // Groups are NOT applied until Enter - no premature chip updates mid-formula.
        function onFormulaChange(): void {
            const text = bar.value;
            // Clearing the box entirely while a filter is applied drops it now,
            // instead of leaving the stale filter active until another trigger.
            if (!text.trim() && (formulaGroups !== null || labelState.size > 0)) {
                clear();
                if (sugg) sugg.hidden = true;
                if (errs) errs.hidden = true;
                return;
            }
            const cursor = bar.selectionStart ?? text.length;
            const tok = currentToken(text, cursor);
            showSuggestions(tok.text);
            availButtons().forEach((btn) => {
                const q = tok.text.toLowerCase();
                btn.style.display = !q || (btn.dataset.labelName || "").includes(q) ? "" : "none";
            });
            if (errs) {
                if (tok.text && isDeadEnd(tok.text)) {
                    errs.textContent = `No label matches "${tok.text}"`;
                    errs.hidden = false;
                } else {
                    errs.hidden = true;
                }
            }
        }

        bar.addEventListener("input", onFormulaChange);

        bar.addEventListener("keydown", (e) => {
            if (e.key === "Tab" || e.key === "ArrowDown") {
                if (sugg && !sugg.hidden) {
                    e.preventDefault();
                    if (activeSuggIdx < 0) selectSuggestion(0);
                    else selectSuggestion(e.key === "ArrowDown" ? 1 : 0);
                    const activeEl = sugg.querySelector<HTMLElement>(".fp-formula-sugg.active");
                    if (activeEl && e.key === "Tab") insertSuggestion(activeEl.dataset.label || "");
                } else if (e.key === "Tab") {
                    const cursor = bar.selectionStart ?? 0;
                    const tok = currentToken(bar.value, cursor);
                    if (tok.text) {
                        const match = availButtons().find((btn) => btn.style.display !== "none");
                        if (match) {
                            e.preventDefault();
                            insertSuggestion(match.dataset.labelText || "");
                        }
                    }
                }
            } else if (e.key === "ArrowUp") {
                if (sugg && !sugg.hidden) {
                    e.preventDefault();
                    selectSuggestion(-1);
                }
            } else if (e.key === "Enter") {
                e.preventDefault();
                if (sugg && !sugg.hidden && activeSuggIdx >= 0) {
                    const activeEl = sugg.querySelector<HTMLElement>(".fp-formula-sugg.active");
                    if (activeEl) {
                        insertSuggestion(activeEl.dataset.label || "");
                        return;
                    }
                }
                const text = bar.value.trim();
                if (!text) {
                    // Empty box + Enter clears any active filter.
                    if (formulaGroups !== null || labelState.size > 0) clear();
                    if (sugg) sugg.hidden = true;
                    if (errs) errs.hidden = true;
                    return;
                }
                const hasOps = /[-\/\+\(\)]/.test(text);
                if (hasOps) {
                    const { groups, errors } = parseTokens(tokenize(text));
                    if (errs) {
                        if (errors.length) {
                            errs.textContent = `Unknown: ${errors.join(", ")}`;
                            errs.hidden = false;
                        } else {
                            errs.hidden = true;
                        }
                    }
                    if (groups.length) {
                        applyGroups(groups);
                        bar.value = "";
                        if (sugg) sugg.hidden = true;
                        availButtons().forEach((b) => {
                            b.style.display = "";
                        });
                        if (errs && !errors.length) errs.hidden = true;
                    }
                } else {
                    // Plain search + Enter: add the first visible label as include.
                    const firstVisible = availButtons().find((btn) => btn.style.display !== "none");
                    if (firstVisible) {
                        addLabel(firstVisible, "incl");
                        bar.value = "";
                        if (sugg) sugg.hidden = true;
                        availButtons().forEach((b) => {
                            b.style.display = "";
                        });
                        if (errs) errs.hidden = true;
                    }
                }
            } else if (e.key === "Escape") {
                if (sugg) sugg.hidden = true;
                if (errs) errs.hidden = true;
            }
        });

        bar.addEventListener("blur", () =>
            setTimeout(() => {
                if (sugg) sugg.hidden = true;
            }, 160),
        );
        bar.addEventListener("focus", () => {
            const tok = currentToken(bar.value, bar.selectionStart ?? 0);
            if (tok.text) showSuggestions(tok.text);
        });
    })();

    // -- Public API -----------------------------------------------------------
    function applyGroups(groups: LabelGroup[]): void {
        // Set BEFORE the state rebuild so serialization preserves structure.
        formulaGroups = groups;
        availButtons().forEach((b) => {
            b.style.display = "";
        });
        labelState.clear();
        inclMode = "and";
        groups.forEach((g) => {
            if (g.op === "not") {
                g.ids.forEach((id) => labelState.set(String(id), "excl"));
            } else {
                g.ids.forEach((id) => labelState.set(String(id), "incl"));
                if (g.op === "or") inclMode = "or";
            }
        });
        labelState.forEach((_mode, id) => {
            const btn = availById(id);
            if (btn) btn.style.display = "none";
        });
        rebuild();
        onChange();
    }

    function mergeIncludeIds(ids: Array<number | string>): void {
        for (const id of ids) {
            const key = String(id);
            if (labelState.get(key) !== "excl") labelState.set(key, "incl");
        }
        // Merged state may mix groups; fall back to simple chip mode.
        formulaGroups = null;
        labelState.forEach((_mode, id) => {
            const btn = availById(id);
            if (btn) btn.style.display = "none";
        });
        rebuild();
    }

    return {
        clear,
        rebuild,
        applyGroups,
        mergeIncludeIds,
        isEmpty: () => labelState.size === 0 && formulaGroups === null,
    };
}

// ---------------------------------------------------------------------------
// Flat search-and-chips picker
// ---------------------------------------------------------------------------

export interface ChipCandidate {
    id: string;
    name: string;
    icon?: string;
    color?: string;
}

export interface ChipPickerOptions {
    chipsEl: HTMLElement;
    searchEl: HTMLInputElement;
    suggEl: HTMLElement;
    /** Maximum suggestions rendered per query (default 12). */
    maxSuggestions?: number;
    /** Fired after every selection change (add or remove). */
    onChange?: () => void;
}

export interface ChipPickerApi {
    setCandidates(list: ChipCandidate[]): void;
    /** Replace the current selection (renders immediately; no onChange). */
    setSelected(list: ChipCandidate[]): void;
    reset(): void;
    getSelectedIds(): string[];
}

export function createChipPicker(options: ChipPickerOptions): ChipPickerApi {
    const { chipsEl, searchEl, suggEl } = options;
    const maxSuggestions = options.maxSuggestions ?? 12;
    const onChange = options.onChange || (() => {});
    let candidates: ChipCandidate[] = [];
    let selected: ChipCandidate[] = [];

    function renderChips(): void {
        chipsEl.innerHTML = "";
        selected.forEach((item) => {
            const chip = document.createElement("span");
            chip.className = "apdlg-label-chip-item";
            chip.dataset.id = item.id;
            if (item.color) chip.style.setProperty("--tag-color", item.color);
            const iconHtml = item.icon ? `<span class="apdlg-chip-icon">${escHtml(item.icon)}</span>` : "";
            chip.innerHTML = `${iconHtml}<span class="apdlg-chip-name">${escHtml(item.name)}</span><button class="apdlg-chip-remove" type="button" aria-label="Remove">x</button>`;
            chip.querySelector(".apdlg-chip-remove")!.addEventListener("click", () => {
                selected = selected.filter((s) => s.id !== item.id);
                renderChips();
                onChange();
            });
            chipsEl.appendChild(chip);
        });
    }

    function renderSuggestions(query: string): void {
        const q = (query || "").toLowerCase().trim();
        const selectedIds = new Set(selected.map((s) => s.id));
        const matches = candidates.filter((c) => !selectedIds.has(c.id) && (!q || c.name.toLowerCase().includes(q))).slice(0, maxSuggestions);
        if (!matches.length) {
            suggEl.hidden = true;
            suggEl.innerHTML = "";
            return;
        }
        suggEl.innerHTML = "";
        matches.forEach((item) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "apdlg-label-sugg-item";
            const iconHtml = item.icon ? `<span class="apdlg-sugg-icon">${escHtml(item.icon)}</span>` : "";
            btn.innerHTML = `${iconHtml}<span class="apdlg-sugg-name">${escHtml(item.name)}</span>`;
            btn.addEventListener("click", () => {
                selected.push(item);
                renderChips();
                searchEl.value = "";
                suggEl.hidden = true;
                searchEl.focus();
                onChange();
            });
            suggEl.appendChild(btn);
        });
        suggEl.hidden = false;
    }

    searchEl.addEventListener("input", () => renderSuggestions(searchEl.value));
    searchEl.addEventListener("focus", () => renderSuggestions(searchEl.value));
    searchEl.addEventListener("blur", () =>
        setTimeout(() => {
            suggEl.hidden = true;
        }, 150),
    );

    return {
        setCandidates(list: ChipCandidate[]): void {
            candidates = list || [];
        },
        setSelected(list: ChipCandidate[]): void {
            selected = [...(list || [])];
            renderChips();
        },
        reset(): void {
            selected = [];
            renderChips();
            searchEl.value = "";
            suggEl.hidden = true;
        },
        getSelectedIds(): string[] {
            return selected.map((s) => s.id);
        },
    };
}

// ---------------------------------------------------------------------------
// Global installation (core.js)
// ---------------------------------------------------------------------------

export interface UrbanLensLabelPickerGlobal {
    createFilterPicker: typeof createFilterPicker;
    createChipPicker: typeof createChipPicker;
}

export function installGlobalLabelPicker(): void {
    (window as unknown as { UrbanLensLabelPicker: UrbanLensLabelPickerGlobal }).UrbanLensLabelPicker = {
        createFilterPicker,
        createChipPicker,
    };
}
