/**
 * Frontend JS build wrapper for the `build`/`deploy` npm scripts.
 */

import { existsSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { version as maplibreVersion } from "maplibre-gl/package.json";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const OUT_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/static/dashboard/js");
const ENTRIES_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/ts/entries");
const ENTRIES_CLASSIC_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/ts/entries-classic");
/** Each package plugin's entries (`<plugin>/frontend/ts/entries/*.ts`) build into its own `static/<plugin>/js/`. */
const PLUGINS_DIR = join(ROOT, "src/urbanlens/plugins");

const minify = process.argv.includes("--minify");

/** Lists the `.ts` files directly inside `dir`, or `[]` if it doesn't exist or has none. */
function tsFiles(dir: string): string[] {
    try {
        return readdirSync(dir)
            // Tests live beside the entry they exercise, and are not pages.
            // A browser test importing bun:test fails the build outright.
            .filter((name) => name.endsWith(".ts") && !name.includes(".test."))
            .map((name) => join(dir, name));
    } catch {
        return [];
    }
}

/** Runs `bun build` over `files` with `extraArgs`, or does nothing if `files` is empty. */
async function buildGroup(files: string[], extraArgs: string[], entryNaming = "[name].[ext]", outDir = OUT_DIR): Promise<void> {
    if (!files.length) return;
    // Bun's default --entry-naming ("[dir]/[name].[ext]") mirrors each entry's source directory under --outdir on some Bun versions.
    const proc = Bun.spawn([process.execPath, "build", ...files, "--outdir", outDir, `--entry-naming=${entryNaming}`, ...extraArgs], {
        stdout: "inherit",
        stderr: "inherit",
    });
    const exitCode = await proc.exited;
    if (exitCode !== 0) process.exit(exitCode);
}

rmSync(OUT_DIR, { recursive: true, force: true });

await buildGroup(tsFiles(ENTRIES_DIR), [
    "--target",
    "browser",
    "--splitting",
    "--format",
    "esm",
    ...(minify ? ["--minify"] : []),
]);

await buildGroup(tsFiles(ENTRIES_CLASSIC_DIR), [
    "--target",
    "browser",
    "--format",
    "iife",
    "--define",
    "import.meta.url='about:blank'",
    ...(minify ? ["--minify"] : []),
]);

// MapLibre's tile worker, which `entries-classic/maplibre-gl.ts` points MapLibre at. Its published form imports a
// shared chunk by relative path, so it is bundled into one file rather than copied; the version in the name is the one
// that entry asks for.
await buildGroup(
    [Bun.resolveSync("maplibre-gl/dist/maplibre-gl-worker.mjs", ROOT)],
    ["--target", "browser", "--format", "esm", ...(minify ? ["--minify"] : [])],
    `[name]-${maplibreVersion}.js`,
);

const pluginNames = existsSync(PLUGINS_DIR) ? readdirSync(PLUGINS_DIR, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => entry.name) : [];
for (const plugin of pluginNames) {
    const entries = tsFiles(join(PLUGINS_DIR, plugin, "frontend/ts/entries"));
    if (!entries.length) continue;
    const outDir = join(PLUGINS_DIR, plugin, "static", plugin, "js");
    rmSync(outDir, { recursive: true, force: true });
    await buildGroup(entries, ["--target", "browser", "--splitting", "--format", "esm", ...(minify ? ["--minify"] : [])], "[name].[ext]", outDir);
}
