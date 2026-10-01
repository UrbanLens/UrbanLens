/** The first-run setup wizard (``pages/setup/index.html``). */

import { SetupWizard } from "../shared/setup-wizard";

const root = document.querySelector<HTMLElement>(".setup-wizard");
if (root) new SetupWizard(root).install();
