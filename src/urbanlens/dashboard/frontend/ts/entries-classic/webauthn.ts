/**
 * WebAuthn globals bundle: exposes window.UrbanLensWebAuthn for the login-2fa page and the Settings > Security passkey panel.
 */
import { registerPasskey, runLogin } from "../shared/webauthn-client";

const api = { registerPasskey, runLogin };

window.UrbanLensWebAuthn = api;

declare global {
    interface Window {
        UrbanLensWebAuthn: typeof api;
    }
}
