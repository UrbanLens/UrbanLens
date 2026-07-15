/**
 * Browser permissions globals bundle: exposes window.UrbanLensPermissions
 * for the Settings > Connections "Browser Permissions" cards.
 *
 * Built as a classic IIFE script (like e2ee.ts and webauthn.ts) so inline
 * <script> blocks in that template can call it synchronously after load.
 */
import {
    getLocationPermissionState,
    getNotificationPermissionState,
    requestLocationPermission,
    requestNotificationPermission,
} from "../shared/permissions-client";

const api = {
    getLocationPermissionState,
    getNotificationPermissionState,
    requestLocationPermission,
    requestNotificationPermission,
};

window.UrbanLensPermissions = api;

declare global {
    interface Window {
        UrbanLensPermissions: typeof api;
    }
}
