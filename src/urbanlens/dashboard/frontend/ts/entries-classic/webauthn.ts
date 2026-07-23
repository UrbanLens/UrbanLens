/**
 * WebAuthn globals bundle: exposes window.UrbanLensWebAuthn for the
 * login-2fa page and the Settings > Security passkey panel.
 *
 * Built as a classic IIFE script (like e2ee.ts and core.ts) so inline
 * <script> blocks in those templates can call it synchronously after load.
 */
import { registerPasskey, runLogin } from "../shared/webauthn-client";

const api = { registerPasskey, runLogin };

window.UrbanLensWebAuthn = api;

declare global {
    interface Window {
        UrbanLensWebAuthn: typeof api;
    }
}
