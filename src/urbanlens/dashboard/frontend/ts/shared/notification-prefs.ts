/**
 * The notification preferences grid on the Settings page (``partials/notifications/notification_preferences.html``),
 * which htmx re-renders on every change: the None column's exclusivity, and the browser-notification section.
 */

interface NotificationApi {
    readonly permission: NotificationPermission;
    requestPermission(): Promise<NotificationPermission>;
}

export type NotificationAccess = NotificationApi | null;

const STATUS: Partial<Record<NotificationPermission, string>> = {
    granted: "Enabled - you'll get desktop notifications while UrbanLens is open in a background tab.",
    denied: "Blocked - allow notifications for this site in your browser settings to turn them on.",
};

function browserNotifications(): NotificationAccess {
    return "Notification" in window ? Notification : null;
}

function boxesFor(from: Element, selector: string, field: string): HTMLInputElement[] {
    const scope = from.closest("form") ?? document;
    return Array.from(scope.querySelectorAll<HTMLInputElement>(selector)).filter((box) => box.dataset.field === field);
}

function onChange(event: Event): void {
    const box = event.target;
    if (!(box instanceof HTMLInputElement)) return;
    const field = box.dataset.field ?? "";
    if (box.matches(".notif-none-box")) {
        if (box.checked) for (const delivery of boxesFor(box, ".notif-delivery-box", field)) delivery.checked = false;
    } else if (box.matches(".notif-delivery-box")) {
        const anyDelivery = boxesFor(box, ".notif-delivery-box", field).some((delivery) => delivery.checked);
        for (const none of boxesFor(box, ".notif-none-box", field)) none.checked = !anyDelivery;
    }
}

function renderBrowserSection(access: NotificationAccess): void {
    const wrap = document.getElementById("notif-browser-prefs");
    const status = document.getElementById("notif-browser-status");
    const button = document.getElementById("notif-browser-enable");
    if (!wrap || !access) return;
    wrap.hidden = false;
    const text = STATUS[access.permission] ?? "";
    if (status) status.textContent = text;
    if (button) {
        button.hidden = Boolean(text);
        // ``.btn`` sets its own display, which outranks the attribute.
        button.style.display = text ? "none" : "";
    }
}

export function installNotificationPrefs(access: () => NotificationAccess = browserNotifications): void {
    document.addEventListener("change", onChange);
    document.addEventListener("click", (event) => {
        const control = event.target instanceof Element ? event.target.closest("[data-notif-action='enable-browser']") : null;
        const notifications = access();
        if (!control || !notifications) return;
        void notifications.requestPermission().then(() => {
            renderBrowserSection(access());
            if (access()?.permission === "granted") window.toastr?.success("Browser notifications enabled.");
        });
    });
    document.body.addEventListener("htmx:afterSettle", () => renderBrowserSection(access()));
    renderBrowserSection(access());
}
