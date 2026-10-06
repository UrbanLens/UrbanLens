/**
 * Registers a DOM for `bun test`.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";

import { setEsriAttributionFetchForTests } from "../shared/esri-attribution";

GlobalRegistrator.register({ url: "https://urbanlens.test/" });

// Any test that draws an Esri basemap would otherwise ask static.arcgis.com for its credits, and
// assert on whatever came back first. One that cares stubs the answer it wants.
setEsriAttributionFetchForTests(() => Promise.reject(new Error("unit tests do not reach static.arcgis.com")));
