/**
 * Browser permission status checks + prompt triggers for the Settings >
 * Connections "Browser Permissions" cards. Wraps the Permissions API where
 * supported (geolocation) and falls back to the permission-specific state
 * (Notification.permission) where the Permissions API doesn't cover it.
 */

export type BrowserPermissionState = "granted" | "denied" | "prompt" | "unsupported";

async function queryState(name: PermissionName): Promise<BrowserPermissionState> {
    if (!navigator.permissions?.query) return "unsupported";
    try {
        const status = await navigator.permissions.query({ name });
        return status.state as BrowserPermissionState;
    } catch {
        return "unsupported";
    }
}

/** Current geolocation permission state, without prompting. */
export function getLocationPermissionState(): Promise<BrowserPermissionState> {
    if (!navigator.geolocation) return Promise.resolve("unsupported");
    return queryState("geolocation");
}

/** Current notification permission state, without prompting. */
export function getNotificationPermissionState(): Promise<BrowserPermissionState> {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    return Promise.resolve(Notification.permission === "default" ? "prompt" : (Notification.permission as BrowserPermissionState));
}

/** Triggers the browser's location-permission prompt (if not already decided) by requesting a fix. */
export function requestLocationPermission(): Promise<BrowserPermissionState> {
    return new Promise((resolve) => {
        if (!navigator.geolocation) {
            resolve("unsupported");
            return;
        }
        navigator.geolocation.getCurrentPosition(
            () => resolve("granted"),
            (error) => resolve(error.code === error.PERMISSION_DENIED ? "denied" : "prompt"),
            { timeout: 10_000 },
        );
    });
}

/** Triggers the browser's notification-permission prompt (if not already decided). */
export async function requestNotificationPermission(): Promise<BrowserPermissionState> {
    if (!("Notification" in window)) return "unsupported";
    const result = await Notification.requestPermission();
    return result === "default" ? "prompt" : (result as BrowserPermissionState);
}
