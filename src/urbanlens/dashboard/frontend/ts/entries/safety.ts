/**
 * The safety check-in pages (``pages/safety/*.html``). Each piece wires itself only where its elements are.
 */

import { installLeaveConfirmation } from "../shared/leave-confirmation";
import { replaceContactPicker, SafetyAutosave } from "../shared/safety-autosave";
import { initContactPickers } from "../shared/safety-contact-picker";
import { installSafetyLiveLocation } from "../shared/safety-live-location";
import { allowLeaving, installAutoDeleteNever, installCheckinTiming, installLiveLocationMarker, installSafetyPage, isLeavingAllowed } from "../shared/safety-page";
import { installSafetyChat } from "../shared/safety-chat";
import { installSafetyMap } from "../shared/safety-map";
import { getCsrfToken } from "../shared/csrf";

function byId<T extends HTMLElement>(id: string, type: new () => T): T | null {
    const el = document.getElementById(id);
    return el instanceof type ? el : null;
}

function installCheckinForm(form: HTMLFormElement): void {
    const pickerContainer = document.getElementById("safety-contact-picker-container");
    new SafetyAutosave({
        form,
        status: document.getElementById("safety-autosave-status"),
        floating: true,
        failureMessage: "Could not save your changes. Please try again.",
        onSaved(data) {
            if (pickerContainer && data.contacts_html) replaceContactPicker(pickerContainer, data.contacts_html);
            const title = document.getElementById("safety-title-text");
            if (title && data.title) title.textContent = data.title;
            // The header's active check-in banner shows the title too.
            window.htmx?.trigger(document.body, "safetyBannerRefresh");
            for (const warning of data.warnings ?? []) window.toastr?.error(warning);
        },
    }).install();

    // Distinct from the autosave guard: this is about the check-in reaching nobody, not an unsaved edit.
    if (form.dataset.resolved === "true") return;
    installLeaveConfirmation({
        isBlocked: () => {
            if (isLeavingAllowed()) return false;
            if (document.querySelector("#safety-contact-picker-container .safety-contact-chip")) return false;
            return !byId("notify_community_wiki", HTMLInputElement)?.checked;
        },
        title: "Leave without a way to reach you?",
        message: "You haven't added any emergency contacts, and community wiki notification is off - if something happens, no one will be notified. Leave anyway, or stay to finish setting this up?",
        confirmLabel: "Leave anyway",
        onConfirmed: allowLeaving,
    });
}

function installDefaultsForm(form: HTMLFormElement): void {
    const autosave = new SafetyAutosave({
        form,
        status: document.getElementById("safety-autosave-status"),
        failureMessage: "Could not save your safety defaults. Please try again.",
        onSaved(data) {
            for (const message of data.rejected_contacts ?? []) window.toastr?.error(message);
            window.toastr?.success("Defaults saved.");
        },
    });
    autosave.install();
    installAutoDeleteNever(() => autosave.schedule(true));
}

installSafetyPage();
initContactPickers();

const checkinForm = byId("safety-checkin-form", HTMLFormElement);
if (checkinForm) installCheckinForm(checkinForm);

const defaultsForm = byId("safety-defaults-form", HTMLFormElement);
if (defaultsForm) installDefaultsForm(defaultsForm);

const createForm = byId("safety-create-form", HTMLFormElement);
const checkinBy = byId("checkin_by", HTMLInputElement);
const checkinByUtc = byId("checkin_by_utc", HTMLInputElement);
if (createForm && checkinBy && checkinByUtc) installCheckinTiming(createForm, checkinBy, checkinByUtc);

const liveToggle = byId("safety-live-location-toggle", HTMLInputElement);
if (liveToggle?.dataset.toggleUrl && liveToggle.dataset.updateUrl) {
    installSafetyLiveLocation({ toggle: liveToggle, toggleUrl: liveToggle.dataset.toggleUrl, updateUrl: liveToggle.dataset.updateUrl, csrfToken: getCsrfToken() });
}

const chatPanel = document.getElementById("safety-chat-panel");
if (chatPanel) installSafetyChat(chatPanel);

// Before the live-location marker, which draws on it.
const safetyMap = document.querySelector<HTMLElement>("[data-safety-map]");
if (safetyMap) installSafetyMap(safetyMap);

const liveCard = document.getElementById("safety-live-location-card");
if (liveCard) installLiveLocationMarker(liveCard);
