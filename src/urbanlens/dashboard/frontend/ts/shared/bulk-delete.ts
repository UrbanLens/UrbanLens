/**
 * Reading the answer to a bulk photo delete (``controllers/image_gallery.py::_delete_owned_images``).
 */

export interface BulkDeleteResult {
    deleted?: number;
    unlinked?: number;
    /** Every photo it acted on. An endpoint skips a selected photo that is not its own to delete, silently. */
    image_ids?: number[];
}

function photos(n: number): string {
    return `${n} photo${n === 1 ? "" : "s"}`;
}

/** Which of the *selected* photos the delete took, and what to tell the viewer about it. */
export function bulkDeleteOutcome(selected: number[], result: BulkDeleteResult | null): { gone: number[]; message: string } {
    const gone = result?.image_ids ?? selected;
    const deleted = result?.deleted ?? gone.length;
    const unlinked = result?.unlinked ?? 0;
    const skipped = selected.length - gone.length;
    const parts = [
        deleted ? `Deleted ${photos(deleted)}.` : "",
        unlinked ? `Removed ${photos(unlinked)} from this pin; still on the wiki.` : "",
        skipped > 0 ? `Left ${photos(skipped)} that can't be deleted from here.` : "",
    ].filter(Boolean);
    return { gone, message: parts.join(" ") || "Nothing was deleted." };
}
