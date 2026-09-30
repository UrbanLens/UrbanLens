/**
 * A pin shared with you (``pages/pin_share/detail.html``).
 */

import { installSharedPinMap } from "../shared/shared-pin-map";

const map = document.getElementById("shared-pin-map");
if (map) installSharedPinMap(map);
