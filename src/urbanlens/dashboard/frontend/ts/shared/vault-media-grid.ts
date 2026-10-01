/**
 * One Vault gallery grid (Photos, Documents): infinite scroll, off-screen pruning, the sort control, and settling
 * placeholder tiles once their upload has been processed.
 */

import { observeProcessingTiles, type ProcessingItem } from "./photo-processing";
import { bindPhotoGrid } from "./photo-virtual-grid";

/** A Vault media kind's naming, from which every element id on its page derives. */
export interface VaultKind {
    /** The server's `MediaKind` value: `#photo-grid`, `.photo-tile`, `#photo-tile-<id>`. */
    readonly kind: string;
    /** `#photos-page`, `#vault-photos-sort`, `#photos-file-input`. */
    readonly plural: string;
}

export type VaultTileRenderer = (raw: Record<string, unknown>) => HTMLElement | null;

export interface VaultGridOptions {
    readonly kind: VaultKind;
    readonly renderTile: VaultTileRenderer;
    /** A tile's `<img>`, pruned while off-screen; null for tiles that hold none. */
    readonly imageSelector: string | null;
    /** Query params sent with every page fetch besides the sort. */
    readonly extraParams?: Record<string, string>;
    /** Runs after a placeholder tile has settled or been dropped. */
    readonly onSettled?: () => void;
}

const SKELETON_COUNT = 6;

/** A loaded tile of *kind*; a skeleton shares the class but has no `data-id`. */
export function vaultTileSelector(kind: VaultKind): string {
    return `.${kind.kind}-tile[data-id]`;
}

export function vaultGridId(kind: VaultKind): string {
    return `${kind.kind}-grid`;
}

export function vaultTileId(kind: VaultKind, id: number): string {
    return `${kind.kind}-tile-${id}`;
}

/** The sort the page's picker shows, or `recent` when the gallery is empty and has none. */
export function activeVaultSort(kind: VaultKind): string {
    const select = document.getElementById(`vault-${kind.plural}-sort`);
    return select instanceof HTMLSelectElement ? select.value : "recent";
}

export function renderVaultSkeletonTile(kind: VaultKind): HTMLElement {
    const li = document.createElement("li");
    li.className = `${kind.kind}-tile ${kind.kind}-tile--skeleton`;
    li.setAttribute("aria-hidden", "true");
    return li;
}

/** Swap a placeholder tile for its settled item, or drop it once the item is gone. */
export function settleVaultTile(el: HTMLElement, item: ProcessingItem | null, renderTile: VaultTileRenderer): void {
    const next = item ? renderTile(item) : null;
    if (next) el.replaceWith(next);
    else el.remove();
}

export class VaultGrid {
    private unbind: (() => void) | null = null;

    constructor(
        readonly element: HTMLElement,
        private readonly options: VaultGridOptions,
    ) {}

    /** The grid on this page for *options*' kind, or null when the gallery is empty. */
    static find(options: VaultGridOptions): VaultGrid | null {
        const element = document.getElementById(vaultGridId(options.kind));
        return element ? new VaultGrid(element, options) : null;
    }

    get kind(): VaultKind {
        return this.options.kind;
    }

    renderTile(raw: Record<string, unknown>): HTMLElement | null {
        return this.options.renderTile(raw);
    }

    /** Settle placeholders, bind paging under the current sort, and re-page whenever the sort changes. */
    init(): void {
        const { renderTile, onSettled } = this.options;
        observeProcessingTiles(this.element, (el, item) => {
            settleVaultTile(el, item, renderTile);
            onSettled?.();
        });
        this.bind(activeVaultSort(this.kind));
        const select = document.getElementById(`vault-${this.kind.plural}-sort`);
        if (select instanceof HTMLSelectElement) select.addEventListener("change", () => this.rebind(select.value));
    }

    /** Re-fetch from scratch under the current sort. */
    refresh(): void {
        this.rebind(activeVaultSort(this.kind));
    }

    private rebind(sort: string): void {
        this.element.querySelectorAll(vaultTileSelector(this.kind)).forEach((el) => el.remove());
        this.element.querySelectorAll(".photo-grid-sentinel").forEach((el) => el.remove());
        this.bind(sort);
    }

    private bind(sort: string): void {
        this.unbind?.();
        this.unbind = bindPhotoGrid(this.element, {
            inAlbum: false,
            itemSelector: vaultTileSelector(this.kind),
            imageSelector: this.options.imageSelector,
            renderTile: this.options.renderTile,
            extraParams: { sort, ...this.options.extraParams },
            skeletonCount: SKELETON_COUNT,
            renderSkeleton: () => renderVaultSkeletonTile(this.kind),
        });
    }
}
