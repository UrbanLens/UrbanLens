/**
 * Browser permissions globals bundle: exposes window.UrbanLensPermissions for the Settings > Connections "Browser Permissions" cards.
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
