/**
 * The Settings page (pages/settings/index.html): tabs, autosaving forms, the security and encryption cards,
 * browser permissions, appearance previews, the map-start preview, markup colour pickers, and keyboard
 * shortcuts.
 *
 * The encryption, passkey and permission calls go through the page's own classic bundles (window.UrbanLensE2EE
 * and friends) so the page keeps one copy of their state.
 */

import { byId } from "../shared/dom";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { e2eeUrlsFromDataset } from "../shared/e2ee-urls";
import { FormAutosave, type FormAutosaveOptions } from "../shared/form-autosave";
import { DEFAULT_HOTKEYS, normalizeCombo } from "../shared/hotkeys";

declare const L: typeof import("leaflet");

function numberOrNull(value: string | undefined): number | null {
    const n = Number.parseFloat(value ?? "");
    return Number.isNaN(n) ? null : n;
}

// -- Tabs ---------------------------------------------------------------------------------------

let previewMap: L.Map | null = null;

function activateTab(name: string, silent = false): void {
    const tabs = document.querySelectorAll<HTMLElement>(".settings-tab");
    if (!Array.from(tabs).some((btn) => btn.dataset.tab === name)) return;
    tabs.forEach((btn) => {
        const active = btn.dataset.tab === name;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll<HTMLElement>(".settings-tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.tabPanel === name));
    // A Leaflet map created in a hidden tab has a zero-size viewport until told otherwise.
    if (name === "map" && previewMap) setTimeout(() => previewMap?.invalidateSize(), 0);
    if (!silent) {
        try {
            history.replaceState(null, "", `#tab-${name}`);
        } catch {
            // The tab still switches; only the deep link is lost.
        }
    }
}

/** ``#tab-<name>`` opens a tab; ``#<element id>`` opens the tab holding that element and scrolls to it. */
function activateFromHash(): void {
    const hash = window.location.hash.replace("#", "");
    if (!hash) return;
    if (hash.startsWith("tab-")) {
        activateTab(hash.slice(4), true);
        return;
    }
    const target = document.getElementById(hash);
    const panel = target?.closest<HTMLElement>(".settings-tab-panel");
    if (!target || !panel?.dataset.tabPanel) return;
    activateTab(panel.dataset.tabPanel, true);
    setTimeout(() => target.scrollIntoView({ block: "start" }), 50);
}

function bindTabs(): void {
    document.querySelectorAll<HTMLElement>(".settings-tab").forEach((btn) => btn.addEventListener("click", () => activateTab(btn.dataset.tab ?? "")));
    activateFromHash();
    window.addEventListener("hashchange", activateFromHash);
}

// -- Security: passkeys, TOTP ------------------------------------------------------------------

/** Delegated on document: #security-settings-section-body is swapped wholesale after every TOTP/backup-code action. */
function bindSecurity(root: HTMLElement): void {
    document.addEventListener("click", (e) => {
        const addBtn = e.target instanceof Element ? e.target.closest<HTMLButtonElement>("#add-passkey-btn") : null;
        if (!addBtn || !window.UrbanLensWebAuthn) return;
        addBtn.disabled = true;
        void window.UrbanLensWebAuthn.registerPasskey({ optionsUrl: root.dataset.passkeyOptionsUrl ?? "", registerUrl: root.dataset.passkeyRegisterUrl ?? "" }).then((result) => {
            addBtn.disabled = false;
            if (result.ok) {
                toast.success("Passkey added.");
                window.location.reload();
            } else {
                toast.error(result.error || "Could not add that passkey.");
            }
        });
    });

    const isRenameInput = (el: EventTarget | null): el is HTMLInputElement => el instanceof HTMLInputElement && el.matches(".passkey-rename-form .passkey-name-input");
    // Autosaves on blur; Enter blurs.
    document.addEventListener("focusout", (e) => {
        const input = e.target;
        if (!isRenameInput(input)) return;
        const form = input.closest<HTMLFormElement>(".passkey-rename-form");
        if (!form) return;
        input.disabled = true;
        fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-CSRFToken": getCsrfToken(), "X-Requested-With": "XMLHttpRequest" } })
            .then((r) => {
                if (!r.ok) toast.error("Could not rename that passkey.");
            })
            .catch(() => toast.error("Could not rename that passkey."))
            .finally(() => {
                input.disabled = false;
            });
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && isRenameInput(e.target)) {
            e.preventDefault();
            e.target.blur();
        }
    });
    document.addEventListener("input", (e) => {
        if (e.target instanceof HTMLInputElement && e.target.id === "totp-confirm-code") e.target.value = e.target.value.replace(/\D/g, "").slice(0, 6);
    });
}

// -- Encryption card and password change -------------------------------------------------------

function bindPasswordChange(e2ee: Window["UrbanLensE2EE"]): void {
    const form = byId("password-change-form", HTMLFormElement);
    if (!form) return;
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const hasCurrent = form.dataset.hasPassword === "1";
        const currentInput = byId("password-current", HTMLInputElement);
        const newInput = byId("password-new", HTMLInputElement);
        const confirmInput = byId("password-confirm", HTMLInputElement);
        const submitBtn = byId("password-change-submit", HTMLButtonElement);
        if (!newInput || !confirmInput || !submitBtn) return;
        const current = currentInput?.value ?? "";
        if (hasCurrent && !current) {
            toast.error("Type your current password first.");
            return;
        }
        if (newInput.value !== confirmInput.value) {
            toast.error("Those passwords don't match.");
            return;
        }
        submitBtn.disabled = true;
        const revokeApiKeys = byId("password-revoke-api-keys", HTMLInputElement)?.checked === true;
        e2ee.changePassword(current, newInput.value, form.dataset.username ?? "", { revokeApiKeys })
            .then((result) => {
                if (!result.ok) {
                    toast.error(result.error || "Could not change your password.");
                    return;
                }
                toast.success(hasCurrent ? "Your password has been changed." : "Your password has been set. You can now log in with it.");
                if (currentInput) currentInput.value = "";
                newInput.value = "";
                confirmInput.value = "";
                if (!hasCurrent || revokeApiKeys) window.location.reload();
            })
            .catch(() => toast.error("Could not change your password. Please try again."))
            .finally(() => {
                submitBtn.disabled = false;
            });
    });
}

function bindEncryptionCard(): void {
    const card = byId("e2ee-card", HTMLElement);
    const e2ee = window.UrbanLensE2EE;
    if (!card || !e2ee) return;
    e2ee.init({ selfSlug: card.dataset.selfSlug ?? null, loginIdentifier: card.dataset.loginIdentifier ?? "", urls: e2eeUrlsFromDataset(card.dataset) });
    bindPasswordChange(e2ee);

    const status = byId("e2ee-status", HTMLElement);
    const viewBtn = byId("e2ee-view-recovery", HTMLButtonElement);
    const unlockBtn = byId("e2ee-unlock", HTMLButtonElement);
    const resetBtn = byId("e2ee-reset", HTMLButtonElement);
    const addPasskeyBtn = byId("e2ee-add-passkey", HTMLButtonElement);
    if (!status || !viewBtn || !unlockBtn || !resetBtn) return;
    const hasPassword = card.dataset.hasPassword === "1";

    void e2ee.getUnlockState().then((state) => {
        if (state === "not-enrolled") {
            status.textContent = "Encryption is not set up yet on your account. Sign in again or open Messages to finish setup.";
            return;
        }
        resetBtn.hidden = false;
        if (state === "unlocked") {
            status.textContent = "Encryption is active and this device can read your messages.";
            viewBtn.hidden = false;
            // Enrolling a passkey needs the decrypted key in hand.
            if (window.PublicKeyCredential && addPasskeyBtn) addPasskeyBtn.hidden = false;
        } else {
            status.textContent = "This device can't read your encrypted messages yet. Unlock it with your passkey, password, or recovery key.";
            unlockBtn.hidden = false;
        }
    });

    addPasskeyBtn?.addEventListener("click", () => {
        addPasskeyBtn.disabled = true;
        e2ee.showPasskeyEnrollDialog(hasPassword)
            .then((result) => {
                if (result.ok) toast.success(result.created ? "Passkey created. Your messages now unlock with it on any device." : "Your existing passkey can now unlock your messages on any device.");
                else if (result.error) toast.error(result.error);
            })
            .catch(() => toast.error("Could not add that passkey. Please try again."))
            .finally(() => {
                addPasskeyBtn.disabled = false;
            });
    });
    viewBtn.addEventListener("click", () => {
        void e2ee.regenerateRecoveryKey().then((display) => {
            if (display) void e2ee.showRecoveryDialog(display);
            else toast.error("Could not generate a recovery key. This device may be locked.");
        });
    });
    unlockBtn.addEventListener("click", () => {
        void e2ee.showUnlockDialog().then((ok) => {
            if (ok) window.location.reload();
        });
    });
    resetBtn.addEventListener("click", () => {
        void e2ee.showResetDialog(hasPassword).then((display) => {
            if (display) void e2ee.showRecoveryDialog(display).then(() => window.location.reload());
        });
    });
}

// -- Browser permissions ------------------------------------------------------------------------

type PermissionState = Awaited<ReturnType<Window["UrbanLensPermissions"]["getLocationPermissionState"]>>;

// A site cannot revoke a permission it was granted; only the browser's own settings can.
const PERMISSION_TEXT: Record<PermissionState, string> = {
    granted: "Enabled. To disable, use your browser's site settings.",
    denied: "Blocked - change this in your browser's site settings to re-enable.",
    prompt: "Not enabled yet.",
    unsupported: "Not supported by this browser.",
};

function bindPermissions(): void {
    const list = byId("browser-permissions-list", HTMLElement);
    const perms = window.UrbanLensPermissions;
    if (!list || !perms) return;
    const handlers: Record<string, { get: () => Promise<PermissionState>; request: () => Promise<PermissionState> }> = {
        location: { get: perms.getLocationPermissionState, request: perms.requestLocationPermission },
        notifications: { get: perms.getNotificationPermissionState, request: perms.requestNotificationPermission },
    };

    list.querySelectorAll<HTMLElement>(".settings-permission-card").forEach((card) => {
        const handler = handlers[card.dataset.permission ?? ""];
        const statusEl = card.querySelector<HTMLElement>("[data-status]");
        const enableBtn = card.querySelector<HTMLButtonElement>("[data-enable]");
        if (!handler || !statusEl || !enableBtn) return;
        const apply = (state: PermissionState): void => {
            statusEl.textContent = PERMISSION_TEXT[state];
            statusEl.classList.toggle("settings-permission-status--granted", state === "granted");
            statusEl.classList.toggle("settings-permission-status--denied", state === "denied");
            enableBtn.hidden = state !== "prompt";
        };
        void handler.get().then(apply);
        enableBtn.addEventListener("click", () => {
            void handler.request().then((state) => {
                apply(state);
                if (state === "granted") toast.success("Permission enabled.");
                if (state === "denied") toast.warning("Permission was denied.");
            });
        });
    });
}

// -- Appearance previews ------------------------------------------------------------------------

/** Marks the chosen tile of a radio group whose tiles carry ``<prefix><value>`` ids. */
function markActiveOption(prefix: string, values: string[], chosen: string): void {
    for (const value of values) byId(`${prefix}${value}`, HTMLElement)?.classList.toggle("active", value === chosen);
}

const GUIDANCE_HELP: Record<string, string> = {
    all: "Walkthrough cards on key pages, plus short hover hints on buttons.",
    tooltips: "Hover hints on buttons only - no walkthrough cards.",
    none: "No walkthroughs or hover hints anywhere on the site.",
};

function previewTheme(theme: string): void {
    const root = byId("html-root", HTMLElement);
    const resolved = theme === "system" ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : theme;
    root?.setAttribute("data-theme", resolved);
    markActiveOption("theme-opt-", ["system", "light", "dark"], theme);
}

function previewGuidance(level: string): void {
    byId("html-root", HTMLElement)?.classList.toggle("tooltips-disabled", level === "none");
    markActiveOption("guidance-opt-", Object.keys(GUIDANCE_HELP), level);
    const detail = byId("guidance-help-detail", HTMLElement);
    if (detail && GUIDANCE_HELP[level]) detail.textContent = GUIDANCE_HELP[level];
}

function bindAppearance(): void {
    document.addEventListener("change", (e) => {
        const input = e.target;
        if (!(input instanceof HTMLInputElement) || input.type !== "radio") return;
        if (input.name === "theme_mode") previewTheme(input.value);
        else if (input.name === "map_dark_mode") markActiveOption("map-dark-opt-", ["system", "light", "dark"], input.value);
        else if (input.name === "guidance_level") previewGuidance(input.value);
    });
}

// -- Map start preview ----------------------------------------------------------------------------

const GPS_ZOOM = 13;
const AUTO_ZOOM = 11;

class MapStartPreview {
    private mode: string;
    private readonly map: L.Map;
    private readonly marker: L.Marker;
    private tiles: L.TileLayer;

    constructor(private readonly el: HTMLElement) {
        const d = el.dataset;
        this.mode = d.mode || "gps";
        const lat = numberOrNull(d.lat);
        const lng = numberOrNull(d.lng);
        // A new account with no pins and no saved centre.
        const start = lat !== null && lng !== null ? L.latLng(lat, lng) : L.latLng(40.7128, -74.006);
        const checkedView = document.querySelector<HTMLInputElement>('input[name="default_map_view"]:checked');

        this.map = L.map(el, { attributionControl: false }).setView(start, numberOrNull(d.zoom) ?? 13);
        this.tiles = window.MapLayers.tileLayer(checkedView?.value ?? "satellite").addTo(this.map);
        this.showAttribution();
        this.marker = L.marker(start, { draggable: true }).addTo(this.map);

        this.marker.on("dragend", () => this.pickCustom(this.marker.getLatLng(), true));
        this.map.on("click", (e: L.LeafletMouseEvent) => {
            this.marker.setLatLng(e.latlng);
            this.pickCustom(e.latlng, true);
        });
        this.map.on("zoomend", () => this.setZoomField(this.map.getZoom()));

        // Outside custom mode the saved custom coordinates must survive untouched.
        if (this.mode === "custom") this.syncCustomFields(start);
        this.setZoomField(this.map.getZoom());
        this.applyMode(this.mode);
    }

    get leafletMap(): L.Map {
        return this.map;
    }

    /** The attribution is the tile source's own HTML, shown below the map instead of on it. */
    private showAttribution(): void {
        const el = byId("map-center-preview-attribution", HTMLElement);
        if (el) el.innerHTML = this.tiles.options.attribution ?? "";
    }

    setTileView(kind: string): void {
        this.map.removeLayer(this.tiles);
        this.tiles = window.MapLayers.tileLayer(kind).addTo(this.map);
        this.showAttribution();
    }

    private setZoomField(zoom: number): void {
        const field = byId("id_map_default_zoom", HTMLInputElement);
        if (field) field.value = String(zoom);
    }

    private syncCustomFields(at: L.LatLng): void {
        const lat = byId("id_map_custom_latitude", HTMLInputElement);
        const lng = byId("id_map_custom_longitude", HTMLInputElement);
        if (lat) lat.value = at.lat.toFixed(6);
        if (lng) lng.value = at.lng.toFixed(6);
    }

    /** Dragging the marker or clicking the map picks that spot, which only a Custom start can keep. */
    private pickCustom(at: L.LatLng, showCoords: boolean): void {
        this.map.setView(at);
        this.syncCustomFields(at);
        const address = byId("map-center-address", HTMLInputElement);
        if (showCoords && address) address.value = `${at.lat.toFixed(6)}, ${at.lng.toFixed(6)}`;
        if (this.mode !== "custom") this.selectMode("custom");
    }

    private applyMode(mode: string): void {
        this.mode = mode;
        const custom = mode === "custom";
        if (custom) this.marker.dragging?.enable();
        else this.marker.dragging?.disable();
        const field = byId("custom-location-field", HTMLElement);
        if (field) field.style.display = custom ? "" : "none";
        // Scoped to this group: other tile groups on the page share values like "auto".
        document.querySelectorAll<HTMLInputElement>('input[name="map_center_mode"]').forEach((radio) => {
            radio.closest(".settings-option-tile")?.classList.toggle("active", radio.value === mode);
        });
    }

    selectMode(mode: string): void {
        const radio = document.querySelector<HTMLInputElement>(`input[name="map_center_mode"][value="${CSS.escape(mode)}"]`);
        if (radio) radio.checked = true;
        this.applyMode(mode);
        if (mode === "gps") {
            this.map.setZoom(GPS_ZOOM);
            this.setZoomField(GPS_ZOOM);
            this.useDeviceLocation();
        } else if (mode === "auto") {
            this.map.setZoom(AUTO_ZOOM);
            this.setZoomField(AUTO_ZOOM);
            // Shows where the pins are; not a custom location, so the custom fields stay as saved.
            const lat = numberOrNull(this.el.dataset.centroidLat);
            const lng = numberOrNull(this.el.dataset.centroidLng);
            if (lat !== null && lng !== null) {
                this.marker.setLatLng([lat, lng]);
                this.map.setView([lat, lng], AUTO_ZOOM);
            }
        }
        // Setting .checked fires no change event, and the autosave listens for one.
        radio?.dispatchEvent(new Event("change", { bubbles: true }));
    }

    geocode(): void {
        const address = byId("map-center-address", HTMLInputElement)?.value.trim();
        const errEl = byId("geocode-error", HTMLElement);
        if (!address || !errEl) return;
        errEl.style.display = "none";
        const showError = (message: string): void => {
            errEl.textContent = message;
            errEl.style.display = "";
        };
        fetch(`${this.el.dataset.geocodeUrl ?? ""}?address=${encodeURIComponent(address)}`)
            .then((r) => r.json() as Promise<{ error?: string; lat?: number; lng?: number }>)
            .then((data) => {
                if (data.error || data.lat == null || data.lng == null) {
                    showError(data.error ?? "Geocoding request failed.");
                    return;
                }
                const at = L.latLng(data.lat, data.lng);
                this.marker.setLatLng(at);
                this.map.setView(at, this.map.getZoom());
                this.syncCustomFields(at);
            })
            .catch(() => showError("Geocoding request failed."));
    }

    /** Outside Custom mode this only previews where the map will open. */
    useDeviceLocation(): void {
        navigator.geolocation?.getCurrentPosition((pos) => {
            const at = L.latLng(pos.coords.latitude, pos.coords.longitude);
            this.marker.setLatLng(at);
            this.map.setView(at, this.map.getZoom());
            if (this.mode === "custom") this.syncCustomFields(at);
        });
    }
}

function bindMapStart(): void {
    const el = byId("map-center-preview", HTMLElement);
    if (!el) return;
    const preview = new MapStartPreview(el);
    previewMap = preview.leafletMap;

    document.addEventListener("change", (e) => {
        const radio = e.target;
        if (!(radio instanceof HTMLInputElement) || radio.name !== "default_map_view") return;
        preview.setTileView(radio.value);
        // The server marked only the saved tile active.
        document.querySelectorAll<HTMLInputElement>('input[name="default_map_view"]').forEach((other) => {
            other.closest(".settings-option-tile")?.classList.toggle("active", other === radio);
        });
    });
    // A click on a tile's label reaches its radio too, as does choosing one with the arrow keys.
    document.addEventListener("click", (e) => {
        if (e.target instanceof HTMLInputElement && e.target.name === "map_center_mode") preview.selectMode(e.target.value);
    });
    document.addEventListener("click", (e) => {
        const action = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-settings-action]")?.dataset.settingsAction : undefined;
        if (action === "geocode") preview.geocode();
        else if (action === "use-location") preview.useDeviceLocation();
    });
    byId("map-center-address", HTMLElement)?.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            preview.geocode();
        }
    });
}

// -- Cluster radius -------------------------------------------------------------------------------

function bindClusterRadius(): void {
    const hidden = document.querySelector<HTMLInputElement>("[data-cluster-radius-field]");
    const wrap = byId("cluster-radius-wrap", HTMLElement);
    const slider = byId("cluster-radius-slider", HTMLInputElement);
    if (!hidden || !wrap || !slider) return;
    document.addEventListener("click", (e) => {
        if (!(e.target instanceof HTMLInputElement) || e.target.name !== "_cluster_mode") return;
        const custom = e.target.value === "custom";
        // Empty means automatic.
        hidden.value = custom ? slider.value : "";
        wrap.style.display = custom ? "" : "none";
        byId("cluster-auto-label", HTMLElement)?.classList.toggle("active", !custom);
        byId("cluster-custom-label", HTMLElement)?.classList.toggle("active", custom);
    });
    slider.addEventListener("input", () => {
        hidden.value = slider.value;
        const display = byId("cluster-radius-display", HTMLElement);
        if (display) display.textContent = slider.value;
    });
}

// -- Markup colour and opacity pickers ----------------------------------------------------------

const PALETTE = ["#e53e3e", "#1d4ed8", "#16a34a", "#d97706", "#7c3aed", "#0f172a", "#f8fafc"];
const HEX = /^#[0-9a-f]{6}$/i;

function rgba(color: string, alpha: number): string {
    if (!HEX.test(color)) return "transparent";
    const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(color.slice(i, i + 2), 16));
    return `rgba(${r},${g},${b},${alpha})`;
}

/** One swatch row + opacity slider writing the hidden ``id_markup_<which>_color``/``_opacity`` fields. */
class ColorOpacityPicker {
    private color: string;

    constructor(
        private readonly which: "fill" | "border",
        private readonly onChange: () => void,
    ) {
        this.color = byId(`id_markup_${which}_color`, HTMLInputElement)?.value || (which === "fill" ? "#e53e3e" : "");
        this.buildSwatches();
        byId(`cop-${which}-opacity`, HTMLElement)?.addEventListener("input", () => {
            this.render();
            onChange();
        });
        this.render(false);
    }

    private swatch(color: string): HTMLButtonElement {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "markup-color-swatch";
        btn.classList.toggle("markup-color-swatch--active", color === this.color);
        if (color) {
            btn.style.background = color;
            if (color === "#f8fafc") btn.style.border = "1px solid #cbd5e1";
        } else {
            btn.title = "None (no border)";
            btn.classList.add("markup-color-swatch--none");
            btn.style.cssText = "background:transparent;border:1px solid #cbd5e1;position:relative;";
            const glyph = document.createElement("span");
            glyph.style.cssText = "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.65rem;color:#9ca3af";
            glyph.textContent = "∅";
            btn.append(glyph);
        }
        btn.addEventListener("click", () => {
            btn.parentElement?.querySelectorAll(".markup-color-swatch").forEach((b) => b.classList.remove("markup-color-swatch--active"));
            btn.classList.add("markup-color-swatch--active");
            this.color = color;
            const field = byId(`id_markup_${this.which}_color`, HTMLInputElement);
            if (field) field.value = color;
            this.render();
            this.onChange();
        });
        return btn;
    }

    private buildSwatches(): void {
        const container = byId(`cop-${this.which}-swatches`, HTMLElement);
        if (!container) return;
        container.replaceChildren(...(this.which === "border" ? [""] : []).concat(PALETTE).map((c) => this.swatch(c)));
    }

    /** Draws the preview; ``writeField`` false leaves the saved opacity field as rendered. */
    private render(writeField = true): void {
        const slider = byId(`cop-${this.which}-opacity`, HTMLInputElement);
        if (!slider) return;
        const opacity = Number.parseInt(slider.value, 10);
        if (writeField) {
            const label = byId(`cop-${this.which}-opacity-val`, HTMLElement);
            if (label) label.textContent = String(opacity);
            const field = byId(`id_markup_${this.which}_opacity`, HTMLInputElement);
            if (field) field.value = String(opacity);
        }
        slider.style.setProperty("--cop-color", this.color ? rgba(this.color, 1) : "rgba(0,0,0,0)");
        const preview = byId(`cop-${this.which}-preview`, HTMLElement);
        if (preview) preview.style.background = this.color ? rgba(this.color, opacity / 100) : "transparent";
    }
}

// -- Autosave ------------------------------------------------------------------------------------

const SETTINGS_AUTOSAVE: FormAutosaveOptions = {
    actionsSelector: ".settings-actions",
    submitSelector: ".settings-save-btn",
    // Hidden fields change programmatically; their writers schedule a save themselves.
    changeDelay: (control) => (control.type === "hidden" ? null : ["text", "email", "number"].includes(control.type) ? 1200 : 0),
    inputDelay: (control) => (["range", "text", "email"].includes(control.type) ? 600 : null),
};

// -- Keyboard shortcuts ---------------------------------------------------------------------------

function formatCombo(combo: string): string {
    return combo
        .split("+")
        .map((part) => (part.length === 1 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1)))
        .join("+");
}

function bindHotkeys(autosave: FormAutosave): void {
    const input = byId("id_keyboard_shortcuts", HTMLInputElement);
    const rows = byId("hotkey-rows", HTMLElement);
    if (!input || !rows) return;
    let overrides: Record<string, string> = {};
    try {
        overrides = (JSON.parse(input.value || "{}") as Record<string, string> | null) ?? {};
    } catch {
        overrides = {};
    }
    const commit = (): void => {
        input.value = JSON.stringify(overrides);
        const form = byId("hotkeys-form", HTMLFormElement);
        if (form) autosave.schedule(form, 0);
    };

    for (const [actionId, def] of Object.entries(DEFAULT_HOTKEYS)) {
        const current = (): string => overrides[actionId] || (def.keys[0] ?? "");
        const row = document.createElement("div");
        row.className = "settings-hotkey-row";
        const info = document.createElement("div");
        info.className = "settings-hotkey-info";
        const label = document.createElement("strong");
        label.textContent = def.label;
        const help = document.createElement("span");
        help.className = "settings-help";
        help.textContent = def.description;
        info.append(label, help);

        const controls = document.createElement("div");
        controls.className = "settings-hotkey-controls";
        const captureBtn = document.createElement("button");
        captureBtn.type = "button";
        captureBtn.className = "settings-hotkey-capture";
        captureBtn.textContent = formatCombo(current());
        const resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "settings-hotkey-reset";
        resetBtn.title = "Reset to default";
        const icon = document.createElement("i");
        icon.className = "material-symbols-outlined";
        icon.textContent = "restart_alt";
        resetBtn.append(icon);
        resetBtn.hidden = !overrides[actionId];
        controls.append(captureBtn, resetBtn);
        row.append(info, controls);

        const listen = (event: KeyboardEvent): void => {
            if (event.key === "Escape") {
                captureBtn.textContent = formatCombo(current());
            } else if (event.key !== "Tab") {
                event.preventDefault();
                overrides[actionId] = normalizeCombo(event);
                captureBtn.textContent = formatCombo(overrides[actionId]);
                resetBtn.hidden = false;
                commit();
            }
            captureBtn.classList.remove("is-listening");
            document.removeEventListener("keydown", listen, true);
        };
        captureBtn.addEventListener("click", () => {
            captureBtn.classList.add("is-listening");
            captureBtn.textContent = "Press a key…";
            document.addEventListener("keydown", listen, true);
        });
        resetBtn.addEventListener("click", () => {
            delete overrides[actionId];
            captureBtn.textContent = formatCombo(current());
            resetBtn.hidden = true;
            commit();
        });
        rows.appendChild(row);
    }
}

// -- Small toggles ----------------------------------------------------------------------------------

/** A ``[data-enabled-by="<checkbox id>"]`` block dims and disables its inputs while that checkbox is off. */
function bindDependentOptions(): void {
    document.querySelectorAll<HTMLElement>("[data-enabled-by]").forEach((block) => {
        const master = byId(block.dataset.enabledBy ?? "", HTMLInputElement);
        if (!master) return;
        const sync = (): void => {
            block.style.opacity = master.checked ? "" : "0.4";
            block.querySelectorAll("input").forEach((inp) => {
                inp.disabled = !master.checked;
            });
        };
        master.addEventListener("change", sync);
        sync();
    });
}

function bindStorageEstimate(): void {
    const select = byId("id_image_downscale_max_dimension", HTMLSelectElement);
    const estimate = byId("storage-estimate", HTMLElement);
    if (!select || !estimate) return;
    const update = (): void => {
        const photos = Number.parseInt(select.selectedOptions[0]?.dataset.estimatedPhotos ?? "", 10);
        estimate.textContent = Number.isNaN(photos) ? "" : `Room for about ${photos.toLocaleString()} more photos at this size.`;
    };
    select.addEventListener("change", update);
    update();
}

// -- Wiring -------------------------------------------------------------------------------------------

function bind(root: HTMLElement): void {
    bindTabs();
    bindSecurity(root);
    bindEncryptionCard();
    bindPermissions();
    bindAppearance();
    bindMapStart();
    bindClusterRadius();
    const autosave = new FormAutosave(SETTINGS_AUTOSAVE);
    document.querySelectorAll<HTMLFormElement>(".settings-form:not([data-ul-no-autosave])").forEach((form) => autosave.attach(form));
    const scheduleMarkup = (): void => {
        const form = byId("markup-defaults-form", HTMLFormElement);
        if (form) autosave.schedule(form, 600);
    };
    new ColorOpacityPicker("fill", scheduleMarkup);
    new ColorOpacityPicker("border", scheduleMarkup);
    bindHotkeys(autosave);
    bindDependentOptions();
    bindStorageEstimate();
}

const root = document.querySelector<HTMLElement>(".settings-page");
if (root) bind(root);
