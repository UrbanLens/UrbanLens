/**
 * Places in common with another explorer (``pages/profile/common_pins.html``).
 */

import { installCommonPinsMap } from "../shared/common-pins-map";

const map = document.getElementById("common-pins-map");
if (map) installCommonPinsMap(map, document.getElementById("common-pins-map-data"));
