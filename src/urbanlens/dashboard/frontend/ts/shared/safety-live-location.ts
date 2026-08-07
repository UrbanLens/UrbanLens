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
 */

export interface LiveLocationOptions {
    /** The sharing checkbox. Its state is reverted if the server refuses. */
    toggle: HTMLInputElement;
    toggleUrl: string;
    updateUrl: string;
    csrfToken: string;
    /** Injectable for tests. */
    geolocation?: Pick<Geolocation, "watchPosition" | "clearWatch">;
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

export interface LiveLocationController {
    /** Test seam: the number of consecutive failed reports. */
    consecutiveFailures(): number;
    stop(): void;
}

export function installSafetyLiveLocation(options: LiveLocationOptions): LiveLocationController {
    const { toggle, toggleUrl, updateUrl, csrfToken } = options;
    const geolocation = options.geolocation ?? (typeof navigator === "undefined" ? undefined : navigator.geolocation);
    const notify = options.notify ?? ((_kind, message) => window.toastr?.error(message));
    const now = options.now ?? (() => Date.now());

    let watchId: number | null = null;
    let lastSentAt = 0;
    let pendingPosition: GeolocationPosition | null = null;
    let sendTimer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let warned = false;

    const post = (url: string, body: FormData): Promise<Response> => fetch(url, { method: "POST", headers: { "X-CSRFToken": csrfToken }, body });

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

    function startWatching(): void {
        if (watchId !== null || !geolocation) return;
        watchId = geolocation.watchPosition(
            onPosition,
            () => notify("error", "Could not get your location - live location sharing may not work."),
            { enableHighAccuracy: true },
        );
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
    }

    toggle.addEventListener("change", () => {
        const enabled = toggle.checked;
        const body = new FormData();
        body.append("enabled", enabled ? "1" : "0");

        // Started optimistically so the switch feels immediate, then undone if the
        // server refuses - otherwise the page claims to be sharing a location that
        // is going nowhere.
        if (enabled) startWatching();
        else stopWatching();

        const refuse = (): void => {
            toggle.checked = !enabled;
            if (enabled) stopWatching();
            notify("error", "Could not update live location sharing.");
        };

        void post(toggleUrl, body)
            .then((response) => {
                if (!response.ok) refuse();
            })
            .catch(refuse);
    });

    if (toggle.checked) startWatching();

    return {
        consecutiveFailures: () => failures,
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
