/**
 * Compile core's stylesheet and each package plugin's.
 *
 * A plugin's `frontend/sass/*.scss` (not partials) compile to its `static/<plugin>/<name>.css`, with core's sass
 * directory on the load path so a plugin can `@use "variables"`. Pass `--dev` for expanded output with source maps.
 */

import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import * as sass from "sass";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const CORE_SASS = join(ROOT, "src/urbanlens/dashboard/frontend/sass");
const PLUGINS_DIR = join(ROOT, "src/urbanlens/plugins");
const dev = process.argv.includes("--dev");

function compile(source: string, target: string): void {
    const result = sass.compile(source, { style: dev ? "expanded" : "compressed", sourceMap: dev, loadPaths: [CORE_SASS] });
    mkdirSync(dirname(target), { recursive: true });
    const mapped = dev && result.sourceMap;
    writeFileSync(target, mapped ? `${result.css}\n/*# sourceMappingURL=${basename(target)}.map */\n` : result.css);
    if (mapped) writeFileSync(`${target}.map`, JSON.stringify(result.sourceMap));
}

compile(join(CORE_SASS, "style.scss"), join(ROOT, "src/urbanlens/dashboard/frontend/static/dashboard/style.css"));

const plugins = existsSync(PLUGINS_DIR) ? readdirSync(PLUGINS_DIR, { withFileTypes: true }).filter((entry) => entry.isDirectory()) : [];
for (const plugin of plugins) {
    const sassDir = join(PLUGINS_DIR, plugin.name, "frontend/sass");
    if (!existsSync(sassDir)) continue;
    for (const file of readdirSync(sassDir).filter((name) => name.endsWith(".scss") && !name.startsWith("_"))) {
        compile(join(sassDir, file), join(PLUGINS_DIR, plugin.name, "static", plugin.name, `${basename(file, ".scss")}.css`));
    }
}
