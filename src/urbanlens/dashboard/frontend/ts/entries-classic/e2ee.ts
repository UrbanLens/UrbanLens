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
    encryptForGroup,
    encryptForPartner,
    enroll,
    enrollOauthIfNeeded,
    ensureConversationKey,
    ensureGroupKey,
    getUnlockOptions,
    getUnlockState,
    init,
    regenerateRecoveryKey,
    resetKeys,
    showRecoveryDialog,
    showUnlockDialog,
    unlockWithPassword,
    unlockWithRecovery,
    wireLoginForm,
    wireResetConfirmForm,
    wireSignupForm,
} from "../shared/e2ee-client";

const api = {
    init,
    wireLoginForm,
    wireSignupForm,
    wireResetConfirmForm,
    enroll,
    enrollOauthIfNeeded,
    getUnlockState,
    getUnlockOptions,
    unlockWithRecovery,
    unlockWithPassword,
    showUnlockDialog,
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
    showRecoveryDialog,
};

window.UrbanLensE2EE = api;

declare global {
    interface Window {
        UrbanLensE2EE: typeof api;
    }
}
