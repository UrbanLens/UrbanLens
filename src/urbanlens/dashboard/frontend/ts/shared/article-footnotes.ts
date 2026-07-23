/** Pure helpers for the article WYSIWYG editor's footnote-reference insertion (see entries/article-wysiwyg.ts). */

/** Highest existing `[^N]` footnote number in the article, so a new one is never reused. */
export function nextReferenceNumber(markdown: string): number {
    const pattern = /\[\^(\d+)\]/g;
    let max = 0;
    let match: RegExpExecArray | null = pattern.exec(markdown);
    while (match !== null) {
        max = Math.max(max, parseInt(match[1] ?? "0", 10) || 0);
        match = pattern.exec(markdown);
    }
    return max + 1;
}

/** The `\n[^N]: ` definition stub appended to the end of the article for a new reference marker. */
export function referenceDefinitionStub(n: number, currentContent: string): string {
    return `${currentContent.endsWith("\n") ? "" : "\n"}\n[^${n}]: `;
}
