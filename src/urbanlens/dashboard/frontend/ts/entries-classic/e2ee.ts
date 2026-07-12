/**
 * E2EE globals bundle: exposes window.UrbanLensE2EE for the auth pages
 * (login/signup/password-reset) and the messages page.
 *
 * Built as a classic IIFE script (like core.ts) so inline <script> blocks in
 * those templates can call it synchronously after load. Templates must call
 * UrbanLensE2EE.init({...}) with the endpoint URLs before any other function.
 */
import {
    decryptDom,
    decryptFromPartner,
    encryptForPartner,
    enroll,
    enrollOauthIfNeeded,
    ensureConversationKey,
    getUnlockState,
    init,
    regenerateRecoveryKey,
    resetKeys,
    showRecoveryDialog,
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
    unlockWithRecovery,
    regenerateRecoveryKey,
    resetKeys,
    ensureConversationKey,
    encryptForPartner,
    decryptFromPartner,
    decryptDom,
    showRecoveryDialog,
};

window.UrbanLensE2EE = api;

declare global {
    interface Window {
        UrbanLensE2EE: typeof api;
    }
}
