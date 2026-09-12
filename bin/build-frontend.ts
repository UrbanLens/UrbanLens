/**
 * Frontend JS build wrapper for the `build`/`deploy` npm scripts.
 */

import { readdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const OUT_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/static/dashboard/js");
const ENTRIES_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/ts/entries");
const ENTRIES_CLASSIC_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/ts/entries-classic");

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
async function buildGroup(files: string[], extraArgs: string[]): Promise<void> {
    if (!files.length) return;
    // Bun's default --entry-naming ("[dir]/[name].[ext]") mirrors each entry's source directory under --outdir on some Bun versions.
    const proc = Bun.spawn([process.execPath, "build", ...files, "--outdir", OUT_DIR, "--entry-naming=[name].[ext]", ...extraArgs], {
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
