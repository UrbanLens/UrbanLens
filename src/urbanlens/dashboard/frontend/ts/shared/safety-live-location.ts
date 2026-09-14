/**
 * Live location sharing for a safety check-in.
 */

export interface LiveLocationOptions {
    /** The sharing checkbox. Its state is reverted if the server refuses. */
    toggle: HTMLInputElement;
    toggleUrl: string;
    updateUrl: string;
    csrfToken: string;
    /** Injectable for tests. Explicit null means "this browser has none". */
    geolocation?: Pick<Geolocation, "watchPosition" | "clearWatch"> | null;
    notify?: (kind: "error", message: string) => void;
    /** Clock source. Injectable so tests can cross the throttle window. */
    now?: () => number;
}

/**
 * 30s balances "a partner watching in an emergency wants a current position" against battery and bandwidth.
 */
const MIN_INTERVAL_MS = 30000;

/**
 * How many position reports must fail in a row before saying so.
 */
const FAILURES_BEFORE_WARNING = 2;

/**
 * ``GeolocationPositionError.PERMISSION_DENIED``. Spelled out because the
 * global that carries the constant is not defined in every test environment.
 */
const PERMISSION_DENIED = 1;

export interface LiveLocationController {
    /** Test seam: the number of consecutive failed reports. */
    consecutiveFailures(): number;
    /** Test seam: whether positions are currently being watched. */
    isWatching(): boolean;
    stop(): void;
}

export function installSafetyLiveLocation(options: LiveLocationOptions): LiveLocationController {
    const { toggle, toggleUrl, updateUrl, csrfToken } = options;
    const geolocation = options.geolocation !== undefined ? options.geolocation : typeof navigator === "undefined" ? undefined : navigator.geolocation;
    const notify = options.notify ?? ((_kind, message) => window.toastr?.error(message));
    const now = options.now ?? (() => Date.now());

    let watchId: number | null = null;
    let lastSentAt = 0;
    let pendingPosition: GeolocationPosition | null = null;
    let sendTimer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let warned = false;
    let geoWarned = false;
    /**
 * The sharing state we want the server to hold, waiting to be sent.
 */
    let pendingIntent: { enabled: boolean; onRefused: () => void } | null = null;
    let flushing = false;

    const post = (url: string, body: FormData): Promise<Response> => fetch(url, { method: "POST", headers: { "X-CSRFToken": csrfToken }, body });

    /**
 * Ask the server for a sharing state, superseding any write not yet sent.
 * @param enabled - The state to converge on.
 * @param onRefused - Called if the server refuses *and* nothing newer has
 */
    function requestSharingState(enabled: boolean, onRefused: () => void): void {
        pendingIntent = { enabled, onRefused };
        if (!flushing) void flushSharingState();
    }

    async function flushSharingState(): Promise<void> {
        flushing = true;
        try {
            while (pendingIntent) {
                const intent = pendingIntent;
                pendingIntent = null;
                const body = new FormData();
                body.append("enabled", intent.enabled ? "1" : "0");
                let ok = false;
                try {
                    ok = (await post(toggleUrl, body)).ok;
                } catch {
                    ok = false;
                }
                if (!ok && !pendingIntent) intent.onRefused();
            }
        } finally {
            flushing = false;
        }
    }

    /**
 * Switch sharing off here and on the server, and say why.
 */
    function disableSharing(message: string): void {
        stopWatching();
        toggle.checked = false;
        notify("error", message);
        requestSharingState(false, forcedOffRefused);
    }

    /**
 * The server would not accept the forced "off".
 */
    function forcedOffRefused(): void {
        notify("error", "Live location is still switched on for this check-in. Reload the page and turn it off, or your partner will keep seeing your last position.");
    }

    function onWatchError(error: GeolocationPositionError): void {
        if (error?.code === PERMISSION_DENIED) {
            disableSharing("Location permission is off, so your live location isn't being shared. Allow location for this site, then turn sharing back on.");
            return;
        }
        // A timeout or a momentarily unavailable fix usually recovers by itself,
        // so say it once rather than on every retry.
        if (geoWarned) return;
        geoWarned = true;
        notify("error", "Could not get your location - live location sharing may not work.");
    }

    function reportOutcome(ok: boolean): void {
        if (ok) {
            // Recover quietly: having said it was broken, saying so again after the
            // next blip is only useful if it stayed broken.
            failures = 0;
            warned = false;
            return;
        }
        failures += 1;
        if (failures < FAILURES_BEFORE_WARNING || warned) return;
        warned = true;
        notify("error", "Your live location isn't reaching the server - your partner may be seeing an old position.");
    }

    function sendPosition(position: GeolocationPosition): void {
        lastSentAt = now();
        const body = new FormData();
        body.append("latitude", String(position.coords.latitude));
        body.append("longitude", String(position.coords.longitude));
        if (position.coords.accuracy != null) body.append("accuracy", String(position.coords.accuracy));

        void post(updateUrl, body)
            .then((response) => reportOutcome(response.ok))
            .catch(() => reportOutcome(false));
    }

    function onPosition(position: GeolocationPosition): void {
        const elapsed = now() - lastSentAt;
        if (elapsed >= MIN_INTERVAL_MS) {
            sendPosition(position);
            return;
        }
        // Hold the newest position and send it when the interval is up, so a burst
        // of updates costs one request and still reports the latest fix.
        pendingPosition = position;
        if (sendTimer) return;
        sendTimer = setTimeout(() => {
            sendTimer = null;
            if (!pendingPosition) return;
            sendPosition(pendingPosition);
            pendingPosition = null;
        }, MIN_INTERVAL_MS - elapsed);
    }

    /** @returns Whether positions are being watched; false means they never will be. */
    function startWatching(): boolean {
        if (watchId !== null) return true;
        if (!geolocation) {
            disableSharing("This browser won't share your location, so live location sharing is off.");
            return false;
        }
        watchId = geolocation.watchPosition(onPosition, onWatchError, { enableHighAccuracy: true });
        return true;
    }

    function stopWatching(): void {
        if (watchId !== null && geolocation) {
            geolocation.clearWatch(watchId);
            watchId = null;
        }
        if (sendTimer) clearTimeout(sendTimer);
        sendTimer = null;
        pendingPosition = null;
        failures = 0;
        warned = false;
        geoWarned = false;
    }

    toggle.addEventListener("change", () => {
        const enabled = toggle.checked;

        // Started optimistically so the switch feels immediate, then undone if the server refuses.
        if (enabled) {
            // When the watch cannot run at all, disableSharing has already reset
            // the toggle and told the server, so there is nothing left to send.
            if (!startWatching()) return;
        } else {
            stopWatching();
        }

        const refuse = (): void => {
            toggle.checked = !enabled;
            if (enabled) stopWatching();
            notify("error", "Could not update live location sharing.");
        };

        requestSharingState(enabled, refuse);
    });

    if (toggle.checked) startWatching();

    return {
        consecutiveFailures: () => failures,
        isWatching: () => watchId !== null,
        stop: stopWatching,
    };
}

declare global {
    interface Window {
        ulInstallSafetyLiveLocation?: typeof installSafetyLiveLocation;
    }
}

export function installGlobalSafetyLiveLocation(): void {
    window.ulInstallSafetyLiveLocation = installSafetyLiveLocation;
}
