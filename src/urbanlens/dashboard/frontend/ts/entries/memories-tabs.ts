/**
 * The Memories review tabs: Locations, Visits and Maps. Each piece wires itself only where its elements are.
 */

import { installLocationsTab, installMapsTab, installVisitsTab } from "../shared/memories-tabs";

const locationsMap = document.getElementById("pin-suggestions-map");
if (locationsMap) installLocationsTab(locationsMap);

const visitsMap = document.getElementById("unlogged-visits-map");
if (visitsMap) installVisitsTab(visitsMap);

const mapsPage = document.getElementById("memories-maps-page");
if (mapsPage) installMapsTab(mapsPage);
