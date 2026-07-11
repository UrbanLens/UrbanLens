import { htmxProcess } from "./dialogs";

export interface TreeViewConfig {
    /** CSS selector for a single card, e.g. '.tag-card[data-category-id]'. */
    cardSelector: string;
    /** dataset key (camelCase) holding the card's own id, e.g. 'categoryId'. */
    idKey: string;
    /** dataset key (camelCase) holding a comma-separated list of parent ids. */
    parentsKey: string;
    /** class name applied to the generated tree wrapper, e.g. 'tag-tree-root'. */
    treeRootClass?: string;
}

const DEFAULT_TREE_ROOT_CLASS = "tag-tree-root";

/**
 * Rebuilds a flat list of cards into a parent/child tree by cloning each card
 * into a nested `.tag-tree-item` wrapper, based on `data-*-parents` (a
 * comma-joined id list already rendered server-side). Shared by
 * categories/tags/organize's tag+category tabs (status/people already used a
 * single generic copy of this before this migration).
 */
export function renderTreeView(rows: HTMLElement, config: TreeViewConfig): void {
    const treeRootClass = config.treeRootClass ?? DEFAULT_TREE_ROOT_CLASS;
    rows.querySelector(`.${treeRootClass}`)?.remove();

    const cards = Array.from(rows.querySelectorAll<HTMLElement>(config.cardSelector));
    const cardMap = new Map<string, HTMLElement>();
    const parentMap = new Map<string, string[]>();

    cards.forEach((card) => {
        const id = card.dataset[config.idKey];
        if (!id) return;
        cardMap.set(id, card);
        const parents = card.dataset[config.parentsKey] ?? "";
        parentMap.set(
            id,
            parents
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
        );
        card.style.display = "none";
    });

    const childrenMap = new Map<string, string[]>();
    parentMap.forEach((parents, id) => {
        parents.forEach((pid) => {
            const siblings = childrenMap.get(pid) ?? [];
            siblings.push(id);
            childrenMap.set(pid, siblings);
        });
    });

    const cardIds = new Set(cardMap.keys());
    const rootIds = Array.from(cardMap.keys()).filter((id) => {
        const parents = parentMap.get(id) ?? [];
        return parents.length === 0 || parents.every((pid) => !cardIds.has(pid));
    });
    rootIds.sort((a, b) => cards.indexOf(cardMap.get(a)!) - cards.indexOf(cardMap.get(b)!));

    const treeRoot = document.createElement("div");
    treeRoot.className = treeRootClass;
    const appearedInTree = new Set<string>();

    function buildNode(id: string, depth: number, ancestorPath: Set<string>): HTMLElement | null {
        if (ancestorPath.has(id)) return null;
        const card = cardMap.get(id);
        if (!card) return null;
        appearedInTree.add(id);

        const item = document.createElement("div");
        item.className = "tag-tree-item";
        item.dataset.depth = String(depth);
        item.style.setProperty("--tree-depth", String(depth));

        const clone = card.cloneNode(true) as HTMLElement;
        clone.style.display = "";
        clone.id = `tree-node-${id}-d${depth}-${Math.random().toString(36).slice(2, 6)}`;
        item.appendChild(clone);

        const newPath = new Set(ancestorPath);
        newPath.add(id);

        const children = childrenMap.get(id) ?? [];
        if (children.length > 0) {
            const childrenContainer = document.createElement("div");
            childrenContainer.className = "tag-tree-children";
            children.forEach((cid) => {
                const childNode = buildNode(cid, depth + 1, newPath);
                if (childNode) childrenContainer.appendChild(childNode);
            });
            item.appendChild(childrenContainer);
        }
        return item;
    }

    rootIds.forEach((id) => {
        const node = buildNode(id, 0, new Set());
        if (node) treeRoot.appendChild(node);
    });
    cardMap.forEach((_card, id) => {
        if (!appearedInTree.has(id)) {
            const node = buildNode(id, 0, new Set());
            if (node) treeRoot.appendChild(node);
        }
    });

    rows.appendChild(treeRoot);
    htmxProcess(treeRoot);
}
