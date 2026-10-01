/** The Tools pages (``pages/tools/index.html``, ``admin.html``). */

import { installExportProgress, installExportSelectAll, installImportPicker, installManualBackup, installSectionTabs } from "../shared/tools-page";

installSectionTabs();
installExportSelectAll();
installExportProgress();
installImportPicker();
installManualBackup();
