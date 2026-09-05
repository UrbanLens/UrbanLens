/**
 * E2EE globals bundle: exposes window.UrbanLensE2EE for the auth pages
 * (login/signup/password-reset) and the messages page.
 *
 * Built as a classic IIFE script (like core.ts) so inline <script> blocks in
 * those templates can call it synchronously after load. Templates must call
 * UrbanLensE2EE.init({...}) with the endpoint URLs before any other function.
 */
import {
    changePassword,
    decryptDom,
    decryptFromGroup,
    decryptFromPartner,
    decryptSafetyArchive,
    encryptForGroup,
    encryptForPartner,
    enroll,
    enrollOauthIfNeeded,
    enrollPasskeyUnlock,
    ensureConversationKey,
    ensureGroupKey,
    getUnlockOptions,
    getUnlockState,
    init,
    regenerateRecoveryKey,
    resetKeys,
    showPasskeyEnrollDialog,
    showRecoveryDialog,
    showResetDialog,
    showUnlockDialog,
    unlockFromLoginAssertion,
    unlockWithPasskey,
    unlockWithPassword,
    unlockWithRecovery,
    wireLoginForm,
    wireResetConfirmForm,
    wireSignOutForm,
    wireSignupForm,
} from "../shared/e2ee-client";

const api = {
    init,
    wireLoginForm,
    wireSignupForm,
    wireResetConfirmForm,
    wireSignOutForm,
    enroll,
    enrollOauthIfNeeded,
    enrollPasskeyUnlock,
    getUnlockState,
    getUnlockOptions,
    unlockWithRecovery,
    unlockWithPassword,
    unlockWithPasskey,
    unlockFromLoginAssertion,
    showUnlockDialog,
    showPasskeyEnrollDialog,
    changePassword,
    regenerateRecoveryKey,
    resetKeys,
    ensureConversationKey,
    encryptForPartner,
    decryptFromPartner,
    ensureGroupKey,
    encryptForGroup,
    decryptFromGroup,
    decryptDom,
    decryptSafetyArchive,
    showRecoveryDialog,
    showResetDialog,
};

window.UrbanLensE2EE = api;

// Wired here rather than from the header template: this bundle already loads on
// every signed-in page (themes/base.html), and inline template JavaScript is
// outside every automated check in this repository (see P34). The form is the
// one in partials/layout/header.html; a page without it is a page with nothing
// to clear.
const signOutForm = document.querySelector<HTMLFormElement>("form.nav-dropdown-logout-form");
if (signOutForm) wireSignOutForm(signOutForm);

declare global {
    interface Window {
        UrbanLensE2EE: typeof api;
    }
}
