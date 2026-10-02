/**
 * A native `<select>` popup is legible in both themes (P51).
 *
 * Chromium draws each row on the option's own background, falling back to the select's, and to a white
 * popup when neither is opaque. The site's inputs use a translucent background, so a dark-theme popup
 * came out white under light-grey text. The popup itself is outside the page, so this checks the inputs
 * Chromium paints it from. Its highlighted row takes no author colour, only the select's colour scheme: the
 * dark scheme's is pale blue under the row's light text, so a dark-theme select keeps the light scheme.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

const MIN_CONTRAST = 4.5;

interface PopupRow {
    select: string;
    scheme: string;
    text: string;
    background: string;
    contrast: number;
}

for (const theme of ["dark", "light"] as const) {
    for (const [name, path] of [
        ["settings", appRoutes.settings],
        ["map", appRoutes.map],
    ] as const) {
        test(`every select on the ${name} page opens a legible popup in the ${theme} theme`, async ({ page }) => {
            await page.goto(path);
            await page.locator("#html-root").evaluate((root, value) => root.setAttribute("data-theme", value), theme);

            const rows = await page.evaluate((): PopupRow[] => {
                type Rgba = [number, number, number, number];
                const white: Rgba = [255, 255, 255, 1];
                const probe = document.createElement("canvas").getContext("2d");
                if (!probe) throw new Error("no 2d canvas");
                const rgba = (color: string): Rgba => {
                    probe.clearRect(0, 0, 1, 1);
                    probe.fillStyle = color;
                    probe.fillRect(0, 0, 1, 1);
                    const [r = 0, g = 0, b = 0, a = 0] = probe.getImageData(0, 0, 1, 1).data;
                    return [r, g, b, a / 255];
                };
                const luminance = ([r, g, b]: Rgba): number => {
                    const channel = (value: number): number => {
                        const c = value / 255;
                        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
                    };
                    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
                };
                return [...document.querySelectorAll("select")].flatMap((select) => {
                    const option = select.options[0];
                    if (!option) return [];
                    const optionStyle = getComputedStyle(option);
                    const background = [optionStyle.backgroundColor, getComputedStyle(select).backgroundColor]
                        .map(rgba)
                        .find((color) => color[3] === 1) ?? white;
                    const text = luminance(rgba(optionStyle.color));
                    const fill = luminance(background);
                    return [{
                        select: select.id || select.name || select.className,
                        scheme: getComputedStyle(select).colorScheme,
                        text: optionStyle.color,
                        background: `rgb(${background.slice(0, 3).join(", ")})`,
                        contrast: (Math.max(text, fill) + 0.05) / (Math.min(text, fill) + 0.05),
                    }];
                });
            });

            expect(rows.length, "the page no longer has a select to check").toBeGreaterThan(0);
            const illegible = rows.filter((row) => row.contrast < MIN_CONTRAST);
            expect(illegible, `rows below ${MIN_CONTRAST}:1`).toEqual([]);
            if (theme === "dark") {
                expect(rows.filter((row) => row.scheme !== "light").map((row) => row.select)).toEqual([]);
            }
        });
    }
}

test("the map's filter panel, dark in either theme, draws its controls the same in both", async ({ page }) => {
    await page.goto(appRoutes.map);

    const drawn = async (theme: "light" | "dark"): Promise<string[]> => {
        await page.locator("#html-root").evaluate((root, value) => root.setAttribute("data-theme", value), theme);
        return page.locator("#filter-panel").evaluate((panel) =>
            [...panel.querySelectorAll("input:not([type=hidden]), select, textarea")].map((control) => {
                const style = getComputedStyle(control);
                const name = control.id || control.getAttribute("name");
                // A radio or checkbox shows its label's text, not its own colour.
                if (control.matches("[type=radio], [type=checkbox]")) return [name, style.colorScheme].join(" ");
                const option = control instanceof HTMLSelectElement ? control.options[0] : undefined;
                const optionStyle = option ? getComputedStyle(option) : undefined;
                return [name, style.colorScheme, style.color, optionStyle?.color, optionStyle?.backgroundColor].join(" ");
            }),
        );
    };

    const light = await drawn("light");
    expect(light.length, "the filter panel has no controls to check").toBeGreaterThan(0);
    expect(light).toEqual(await drawn("dark"));
});

test("the filter panel's saved-filter chips and search hint are legible in both themes", async ({ page }) => {
    await page.goto(appRoutes.map);

    for (const theme of ["light", "dark"] as const) {
        await page.locator("#html-root").evaluate((root, value) => root.setAttribute("data-theme", value), theme);
        const result = await page.locator("#filter-panel").evaluate((panel) => {
            const probe = document.createElement("canvas").getContext("2d");
            if (!probe) throw new Error("no 2d canvas");
            const rgba = (color: string): [number, number, number, number] => {
                probe.clearRect(0, 0, 1, 1);
                probe.fillStyle = color;
                probe.fillRect(0, 0, 1, 1);
                const [r = 0, g = 0, b = 0, a = 0] = probe.getImageData(0, 0, 1, 1).data;
                return [r, g, b, a / 255];
            };
            const luminance = ([r, g, b]: number[]): number => {
                const channel = (value: number): number => {
                    const c = (value ?? 0) / 255;
                    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
                };
                return 0.2126 * channel(r ?? 0) + 0.7152 * channel(g ?? 0) + 0.0722 * channel(b ?? 0);
            };
            const over = (top: number[], under: number[]): number[] => {
                const alpha = top[3] ?? 1;
                return [0, 1, 2].map((i) => (top[i] ?? 0) * alpha + (under[i] ?? 0) * (1 - alpha));
            };
            const panelBackground = over(rgba(getComputedStyle(panel).backgroundColor), [0, 0, 0]);
            const chips = [...panel.querySelectorAll<HTMLElement>(".fp-saved-filter-apply")].map((chip) => {
                const style = getComputedStyle(chip);
                const text = luminance(rgba(style.color));
                const fill = luminance(over(rgba(style.backgroundColor), panelBackground));
                return { name: chip.textContent?.trim(), contrast: (Math.max(text, fill) + 0.05) / (Math.min(text, fill) + 0.05) };
            });
            const search = panel.querySelector<HTMLInputElement>("#fp-search");
            const hint = search ? getComputedStyle(search, "::placeholder").color : null;
            return { chips, hintDiffersFromText: !!search && hint !== getComputedStyle(search).color };
        });
        expect(result.chips.filter((chip) => chip.contrast < 4.5), `${theme}: chips below 4.5:1`).toEqual([]);
        expect(result.hintDiffersFromText, `${theme}: the search hint looks like typed text`).toBe(true);
    }
});
