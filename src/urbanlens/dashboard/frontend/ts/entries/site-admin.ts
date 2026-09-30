/**
 * The site admin pages. Each piece wires itself only where its elements are.
 */

import { installApiLimitsPage } from "../shared/api-limits-page";

if (document.querySelector(".api-limits-page")) installApiLimitsPage(document);
