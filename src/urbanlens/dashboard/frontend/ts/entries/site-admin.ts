/**
 * The site admin pages. Each piece wires itself only where its elements are.
 */

import { installApiLimitsPage } from "../shared/api-limits-page";
import { installSiteStatsPage } from "../shared/site-stats-page";

if (document.querySelector(".api-limits-page")) installApiLimitsPage(document);
if (document.getElementById("stats-refresh-badge")) installSiteStatsPage(document);
