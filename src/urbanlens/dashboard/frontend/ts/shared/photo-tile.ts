/**
 * Shared photo-tile payload used by album grids, drag/drop, the lightbox,
 * and the right-click menu. One shape so a tile rendered from HTML and one
 * fetched as JSON behave the same.
 */

export const PHOTO_IDS_TYPE = "application/x-urbanlens-photo-ids";

export interface PhotoTile {
    id: number;
    uuid: string;
    url: string;
    thumbUrl: string;
    caption: string;
    author: string;
    copyright: string;
    sourceUrl: string;
    takenAt: string;
    lat: number | null;
    lng: number | null;
    mine: boolean;
    onWiki: boolean;
    itemId: number | null;
    albumSlug: string | null;
    mapHidden: boolean;
}

export interface LightboxItem {
    url: string;
    thumbUrl: string;
    caption: string;
    author: string;
    copyright: string;
    sourceUrl: string;
    sourceName: string;
    takenAt: string;
    imageId: number;
    uuid: string;
    isMine: boolean;
    canRelevance: boolean;
    relevant: null;
    latitude: number | null;
    longitude: number | null;
    mapHidden: boolean;
}

/** Parse a gallery tile's data attributes into a PhotoTile. */
export function tileFromElement(el: HTMLElement): PhotoTile | null {
    const id = Number.parseInt(el.dataset.id ?? "", 10);
    if (!id) return null;
    const lat = el.dataset.lat ? Number.parseFloat(el.dataset.lat) : Number.NaN;
    const lng = el.dataset.lng ? Number.parseFloat(el.dataset.lng) : Number.NaN;
    return {
        id,
        uuid: el.dataset.uuid ?? "",
        url: el.dataset.url ?? "",
        thumbUrl: el.dataset.thumbUrl || el.dataset.url || "",
        caption: el.dataset.caption ?? "",
        author: el.dataset.author ?? "",
        copyright: el.dataset.copyright ?? "",
        sourceUrl: el.dataset.sourceUrl ?? "",
        takenAt: el.dataset.takenAt ?? "",
        lat: Number.isFinite(lat) ? lat : null,
        lng: Number.isFinite(lng) ? lng : null,
        mine: el.dataset.mine === "true",
        onWiki: el.dataset.onWiki === "true",
        itemId: el.dataset.itemId ? Number.parseInt(el.dataset.itemId, 10) : null,
        albumSlug: el.dataset.albumSlug || null,
        mapHidden: el.dataset.mapHidden === "true",
    };
}

/** JSON from the album items endpoint into a PhotoTile. */
export function tileFromJson(raw: Record<string, unknown>): PhotoTile | null {
    const id = Number(raw.id);
    if (!id) return null;
    const lat = raw.latitude == null ? Number.NaN : Number(raw.latitude);
    const lng = raw.longitude == null ? Number.NaN : Number(raw.longitude);
    return {
        id,
        uuid: String(raw.uuid ?? ""),
        url: String(raw.url ?? ""),
        thumbUrl: String(raw.thumb_url || raw.url || ""),
        caption: String(raw.caption ?? ""),
        author: String(raw.author ?? ""),
        copyright: String(raw.copyright ?? ""),
        sourceUrl: String(raw.source_url ?? ""),
        takenAt: String(raw.taken_at ?? ""),
        lat: Number.isFinite(lat) ? lat : null,
        lng: Number.isFinite(lng) ? lng : null,
        mine: Boolean(raw.is_mine),
        onWiki: Boolean(raw.on_wiki),
        itemId: raw.item_id == null ? null : Number(raw.item_id),
        albumSlug: raw.album_slug ? String(raw.album_slug) : null,
        mapHidden: Boolean(raw.map_hidden),
    };
}

export function lightboxItemFromTile(tile: PhotoTile): LightboxItem {
    return {
        url: tile.url,
        thumbUrl: tile.thumbUrl,
        caption: tile.caption,
        author: tile.author,
        copyright: tile.copyright,
        sourceUrl: tile.sourceUrl,
        sourceName: "",
        takenAt: tile.takenAt,
        imageId: tile.id,
        uuid: tile.uuid,
        isMine: tile.mine,
        canRelevance: false,
        relevant: null,
        latitude: tile.lat,
        longitude: tile.lng,
        mapHidden: tile.mapHidden,
    };
}

/** Collect lightbox items from a grid, returning the list and the clicked index. */
export function lightboxListFromGrid(grid: HTMLElement, clicked: HTMLElement): { list: LightboxItem[]; idx: number } {
    const tiles = Array.from(grid.querySelectorAll<HTMLElement>(".gallery-item[data-id]"));
    const list: LightboxItem[] = [];
    let idx = 0;
    tiles.forEach((el) => {
        const tile = tileFromElement(el);
        if (!tile) return;
        if (el === clicked || el.contains(clicked)) idx = list.length;
        list.push(lightboxItemFromTile(tile));
    });
    return { list, idx };
}

export function applyTileDataset(el: HTMLElement, tile: PhotoTile): void {
    el.dataset.id = String(tile.id);
    el.dataset.uuid = tile.uuid;
    el.dataset.url = tile.url;
    el.dataset.thumbUrl = tile.thumbUrl;
    el.dataset.caption = tile.caption;
    el.dataset.author = tile.author;
    el.dataset.copyright = tile.copyright;
    el.dataset.sourceUrl = tile.sourceUrl;
    el.dataset.takenAt = tile.takenAt;
    el.dataset.lat = tile.lat == null ? "" : String(tile.lat);
    el.dataset.lng = tile.lng == null ? "" : String(tile.lng);
    el.dataset.mine = tile.mine ? "true" : "false";
    el.dataset.onWiki = tile.onWiki ? "true" : "false";
    if (tile.itemId != null) el.dataset.itemId = String(tile.itemId);
    if (tile.albumSlug) el.dataset.albumSlug = tile.albumSlug;
    el.dataset.mapHidden = tile.mapHidden ? "true" : "false";
}

export function renderPhotoTile(tile: PhotoTile, opts: { inAlbum: boolean; albumSlug?: string }): HTMLLIElement {
    const li = document.createElement("li");
    li.className = opts.inAlbum ? "gallery-item album-item" : "gallery-item";
    li.id = `gallery-item-${tile.id}`;
    li.draggable = true;
    applyTileDataset(li, tile);
    if (opts.inAlbum && opts.albumSlug) li.dataset.albumSlug = opts.albumSlug;

    const check = tile.mine
        ? `<button type="button" class="gallery-select-check" title="Select photo" hidden><i class="material-symbols-outlined">check_circle</i></button>`
        : "";
    const remove = opts.inAlbum
        ? `<button type="button" class="album-item-remove" title="Remove from this album" aria-label="Remove from this album" data-image-id="${tile.id}"><i class="material-symbols-outlined">close</i></button>`
        : "";
    const caption = tile.caption ? `<p class="album-item-caption"></p>` : "";
    li.innerHTML = `${check}<button type="button" class="gallery-thumb-btn" data-photo-open><img src="${tile.thumbUrl}" alt="" class="gallery-thumb" loading="lazy" decoding="async"></button>${remove}${caption}`;
    const captionEl = li.querySelector(".album-item-caption");
    if (captionEl) captionEl.textContent = tile.caption;
    const img = li.querySelector("img");
    if (img) img.alt = tile.caption || "Photo";
    return li;
}

/** Skip a tile with no file to show rather than rendering a broken image. */
export function tileHasImage(tile: PhotoTile): boolean {
    return Boolean(tile.thumbUrl || tile.url);
}

export function parsePhotoIds(data: DataTransfer | null): number[] {
    const raw = data?.getData(PHOTO_IDS_TYPE) || data?.getData("text/plain") || "";
    try {
        const parsed = JSON.parse(raw) as unknown;
        if (!Array.isArray(parsed)) return [];
        return parsed.map((value) => Number(value)).filter((id) => Number.isFinite(id) && id > 0);
    } catch {
        return [];
    }
}

export function writePhotoIds(data: DataTransfer, ids: number[]): void {
    const payload = JSON.stringify(ids);
    data.setData(PHOTO_IDS_TYPE, payload);
    data.setData("text/plain", payload);
    data.effectAllowed = "copyMove";
}
