/**
 * Frontend JS build wrapper for the `build`/`deploy` npm scripts.
 *
 * package.json used to shell out directly to `bun build src/.../entries/*.ts
 * --outdir ...` (and the same for entries-classic/). That relies on the shell
 * expanding the `*.ts` glob before `bun build` ever runs - and unlike bash
 * (which falls back to passing the literal, unmatched pattern through when
 * nullglob is off), Bun Shell throws a hard "no matches found" error and
 * aborts the whole script the moment any of those directories has zero
 * matching files. Since this build runs on every container start
 * (src/bin/init.py's `bun run build`/`bun run deploy`), that failure mode
 * takes the whole app down, not just the JS bundle - see docs/PROBLEMS.md
 * and completed.md for the bug report and reproduction.
 *
 * Enumerating entry files in code instead of a shell glob sidesteps this
 * entirely: an empty directory is just "nothing to build here", not a fatal
 * error.
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
            .filter((name) => name.endsWith(".ts"))
            .map((name) => join(dir, name));
    } catch {
        return [];
    }
}

/** Runs `bun build` over `files` with `extraArgs`, or does nothing if `files` is empty. */
async function buildGroup(files: string[], extraArgs: string[]): Promise<void> {
    if (!files.length) return;
    const proc = Bun.spawn([process.execPath, "build", ...files, "--outdir", OUT_DIR, ...extraArgs], {
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
