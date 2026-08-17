/**
 * Live location sharing for a safety check-in.
 *
 * While enabled, the owner's position is reported to the server so a partner
 * watching the check-in sees where they are. Because the whole point is that
 * somebody can find you, a failure here has to be visible: the previous version
 * swallowed every error, so a check-in resolved elsewhere left the browser
 * happily posting positions that the server rejected while the page still said
 * sharing was on and the partner watched a frozen marker.
 *
 * ``fetch`` only rejects on network failure, never on an HTTP error status, so
 * every response is checked explicitly. The update endpoint answers 400 when
 * sharing has been turned off or the check-in has concluded, and 204 both for a
 * recorded position and for one dropped by its own rate limit - a 204 is a
 * success either way, since the client cannot tell and does not need to.
 *
 * The same reasoning covers the browser end of the pipe: if permission is
 * denied, or there is no geolocation at all, no position will ever be sent, so
 * sharing is switched off here and on the server rather than left on to look
 * like it is working.
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
 * 30s balances "a partner watching in an emergency wants a current position"
 * against battery and bandwidth. Someone overdue on a hike is not moving fast
 * enough for tighter updates to tell a partner anything more.
 */
const MIN_INTERVAL_MS = 30000;

/**
 * How many position reports must fail in a row before saying so.
 *
 * One failure is usually a blip and warning about it would train people to
 * ignore the warning. Two in a row, thirty seconds apart, means it is not
 * recovering - which on a safety feature the owner needs to know.
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
    /** The most recent sharing-flag POST, so a forced "off" can queue behind it. */
    let toggleRequest: Promise<void> = Promise.resolve();
    /**
     * Bumped by every sharing decision - the user's own, and a forced disable.
     *
     * A queued write compares this against the value it captured, because
     * waiting behind an in-flight request means the intent it represents can be
     * overtaken: deny permission while the enable POST is still open, then
     * grant it and switch back on, and the queued "off" would land after the
     * new "on" and disable sharing under a checked toggle and a live watcher.
     */
    let toggleGeneration = 0;

    const post = (url: string, body: FormData): Promise<Response> => fetch(url, { method: "POST", headers: { "X-CSRFToken": csrfToken }, body });

    function postToggle(enabled: boolean): Promise<Response> {
        const body = new FormData();
        body.append("enabled", enabled ? "1" : "0");
        const request = post(toggleUrl, body);
        toggleRequest = request.then(
            () => undefined,
            () => undefined,
        );
        return request;
    }

    /**
     * Switch sharing off here and on the server, and say why.
     *
     * Called when the browser will not produce positions at all. Leaving the
     * toggle on would leave the owner - and the partner watching the check-in -
     * believing a position is on its way when none will ever be sent.
     */
    function disableSharing(message: string): void {
        stopWatching();
        toggle.checked = false;
        notify("error", message);
        // Queued behind any in-flight sharing POST so this "off" cannot overtake
        // the "on" it is undoing - and abandoned if a later decision supersedes
        // it while it waits.
        const generation = ++toggleGeneration;
        void toggleRequest
            .then(() => (generation === toggleGeneration ? postToggle(false) : null))
            .then((response) => {
                if (response && !response.ok) forcedOffRefused();
            })
            .catch(forcedOffRefused);
    }

    /**
     * The server would not accept the forced "off".
     *
     * A non-ok status resolves like any other response, so without this the
     * refusal read as success: switched off here, still enabled server-side,
     * partner still looking at the last known position. Since this browser
     * genuinely cannot send positions, the switch stays off and the owner is
     * told how to reach a control that still works - after a reload the toggle
     * renders from the server's state, so it comes back on and can be turned
     * off for real.
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
        // This is now the account's intent, so any disable still waiting behind
        // an in-flight request is stale and must not fire after it.
        toggleGeneration += 1;

        // Started optimistically so the switch feels immediate, then undone if the
        // server refuses - otherwise the page claims to be sharing a location that
        // is going nowhere.
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

        void postToggle(enabled)
            .then((response) => {
                if (!response.ok) refuse();
            })
            .catch(refuse);
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
