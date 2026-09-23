/** The add-pin dialog's selected-label chips and label suggestion rows, built from text nodes. */

/** A label as the server's label-list/label-options endpoints send it (numeric PK). */
export interface LabelCandidate {
    id: number;
    name: string;
    icon?: string;
    color?: string;
    kind?: string;
}

function textSpan(className: string, text: string): HTMLSpanElement {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    return span;
}

/**
 * One selected label, with its remove button.
 *
 * @param label - The label to show.
 * @param onRemove - Called when the remove button is clicked.
 * @returns The chip element.
 */
export function labelChip(label: LabelCandidate, onRemove: () => void): HTMLElement {
    const chip = document.createElement("span");
    chip.className = "apdlg-label-chip-item";
    chip.dataset.id = String(label.id);
    if (label.icon) chip.append(textSpan("apdlg-chip-icon", label.icon));
    chip.append(textSpan("apdlg-chip-name", label.name));
    const remove = document.createElement("button");
    remove.className = "apdlg-chip-remove";
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove");
    remove.textContent = "x";
    remove.addEventListener("click", onRemove);
    chip.append(remove);
    return chip;
}

/**
 * One suggested label; selecting on mousedown so the search input's blur does not hide it first.
 *
 * @param label - The label to offer.
 * @param onSelect - Called when the row is chosen.
 * @returns The suggestion button.
 */
export function labelSuggestion(label: LabelCandidate, onSelect: () => void): HTMLButtonElement {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "apdlg-label-sugg-item";
    if (label.icon) item.append(textSpan("apdlg-sugg-icon", label.icon));
    item.append(textSpan("apdlg-sugg-name", label.name));
    const kind = textSpan("apdlg-sugg-kind", label.kind ?? "");
    // classList.add throws on whitespace, so only a token-shaped kind becomes a modifier class.
    if (label.kind && /^[\w-]+$/.test(label.kind)) kind.classList.add(`apdlg-sugg-kind--${label.kind}`);
    item.append(kind);
    item.addEventListener("mousedown", (e) => {
        e.preventDefault();
        onSelect();
    });
    return item;
}
