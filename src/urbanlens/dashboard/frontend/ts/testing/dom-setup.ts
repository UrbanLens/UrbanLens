/**
 * Registers a DOM for `bun test`.
 *
 * The frontend had no way to test anything that touches the document: `bun test`
 * ran fine, but only over modules that never asked for `window`. That is the
 * reason 19,638 lines of inline template JS could not be moved into typechecked
 * modules with any confidence - a behavioural change to them could be
 * typechecked and never *exercised*, and the suite would pass either way.
 *
 * Loaded via `bunfig.toml`'s `preload`, so every test file gets a document
 * without importing anything.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";

GlobalRegistrator.register({ url: "https://urbanlens.test/" });
