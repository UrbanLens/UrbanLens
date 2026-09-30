/**
 * The site admin pages. Each piece wires itself only where its elements are.
 */

import { installApiLimitsPage } from "../shared/api-limits-page";
import { installSiteStatsPage } from "../shared/site-stats-page";
import { installSubscriptionsPage } from "../shared/subscriptions-page";
import { FormAutosave } from "../shared/form-autosave";

if (document.querySelector(".api-limits-page")) installApiLimitsPage(document);
if (document.getElementById("stats-refresh-badge")) installSiteStatsPage(document);
if (document.querySelector(".subscription-admin-page")) installSubscriptionsPage(document);

const SITE_SETTINGS_AUTOSAVE = new FormAutosave({
    actionsSelector: ".form-actions",
    submitSelector: ".btn--submit",
    // The name-source priority list writes its order into a hidden input and fires change.
    changeDelay: (control) => (control.type === "hidden" ? (control.id === "default-name-source-priority" ? 0 : null) : control.type === "text" || control.type === "number" ? 1200 : 0),
    inputDelay: (control) => (control.type === "text" || control.type === "number" ? 1200 : null),
});
for (const form of document.querySelectorAll<HTMLFormElement>("form[data-autosave]")) SITE_SETTINGS_AUTOSAVE.attach(form);
