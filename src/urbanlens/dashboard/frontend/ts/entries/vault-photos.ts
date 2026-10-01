// Vault > Photos: gallery grid, uploads, organize queue, the create-pin dialog and the albums menu.
import { installPhotoPinConfirm } from "../shared/photo-pin-confirm";
import { initVaultAlbumsMenu } from "../shared/vault-albums-menu";
import { initVaultPhotosPage } from "../shared/vault-photo-grid";

function boot(): void {
    initVaultPhotosPage();
    installPhotoPinConfirm();
    initVaultAlbumsMenu();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
