/**
 * Passkey (WebAuthn) registration and login-time authentication ceremonies.
 *
 * Mirrors e2ee-client.ts's shape: this module owns every fetch to the
 * passkey endpoints and every navigator.credentials.* call; templates only
 * wire DOM events to the functions exposed on window.UrbanLensWebAuthn (see
 * entries-classic/webauthn.ts). The option/verification JSON shapes are the
 * standard WebAuthn base64url encoding produced by py_webauthn's
 * options_to_json() and expected by its verify_*_response() (see
 * services/webauthn.py) - hand-rolled here (rather than relying on
 * PublicKeyCredential.parseCreationOptionsFromJSON()/toJSON()) for broader
 * browser support.
 */

function base64urlToBuffer(value: string): ArrayBuffer {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/");
    const padding = "=".repeat((4 - (padded.length % 4)) % 4);
    const raw = atob(padded + padding);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) {
        bytes[i] = raw.charCodeAt(i);
    }
    return bytes.buffer;
}

function bufferToBase64url(buffer: ArrayBuffer): string {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]!);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function csrfToken(): string {
    // window.csrftoken is set by a page-level <script> on most dashboard pages,
    // but not on the minimal auth_base.html layout the 2FA login challenge
    // (login_2fa.html) uses - falling back to the csrftoken cookie directly
    // (Django's own documented AJAX pattern) means this works regardless of
    // which layout the calling page happens to use. Without this fallback,
    // runLogin()'s fetch calls here sent an empty X-CSRFToken header on that
    // page, which Django's CSRF middleware always rejected with a 403 HTML
    // error page - silently swallowed by safeJson() into the generic
    // "Could not start passkey sign-in." message, since there was no JSON
    // body to read an error out of.
    if (window.csrftoken) return window.csrftoken;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]!) : "";
}

interface CredentialDescriptorJSON {
    id: string;
    type: string;
    transports?: AuthenticatorTransport[];
}

interface RegistrationOptionsJSON {
    rp: { id?: string; name: string };
    user: { id: string; name: string; displayName: string };
    challenge: string;
    pubKeyCredParams: PublicKeyCredentialParameters[];
    timeout?: number;
    excludeCredentials?: CredentialDescriptorJSON[];
    authenticatorSelection?: AuthenticatorSelectionCriteria;
    attestation?: AttestationConveyancePreference;
}

interface AuthenticationOptionsJSON {
    challenge: string;
    timeout?: number;
    rpId?: string;
    allowCredentials?: CredentialDescriptorJSON[];
    userVerification?: UserVerificationRequirement;
}

function creationOptionsFromJSON(json: RegistrationOptionsJSON): CredentialCreationOptions {
    return {
        publicKey: {
            rp: json.rp,
            user: {
                id: base64urlToBuffer(json.user.id),
                name: json.user.name,
                displayName: json.user.displayName,
            },
            challenge: base64urlToBuffer(json.challenge),
            pubKeyCredParams: json.pubKeyCredParams,
            timeout: json.timeout,
            excludeCredentials: (json.excludeCredentials ?? []).map((cred) => ({
                id: base64urlToBuffer(cred.id),
                type: "public-key" as const,
                transports: cred.transports,
            })),
            authenticatorSelection: json.authenticatorSelection,
            attestation: json.attestation,
        },
    };
}

function requestOptionsFromJSON(json: AuthenticationOptionsJSON): CredentialRequestOptions {
    return {
        publicKey: {
            challenge: base64urlToBuffer(json.challenge),
            timeout: json.timeout,
            rpId: json.rpId,
            allowCredentials: (json.allowCredentials ?? []).map((cred) => ({
                id: base64urlToBuffer(cred.id),
                type: "public-key" as const,
                transports: cred.transports,
            })),
            userVerification: json.userVerification,
        },
    };
}

function credentialToJSON(credential: PublicKeyCredential): Record<string, unknown> {
    const base = {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    };
    const response = credential.response;
    if (response instanceof AuthenticatorAttestationResponse) {
        return {
            ...base,
            response: {
                clientDataJSON: bufferToBase64url(response.clientDataJSON),
                attestationObject: bufferToBase64url(response.attestationObject),
                transports: response.getTransports ? response.getTransports() : undefined,
            },
        };
    }
    const assertion = response as AuthenticatorAssertionResponse;
    return {
        ...base,
        response: {
            clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
            authenticatorData: bufferToBase64url(assertion.authenticatorData),
            signature: bufferToBase64url(assertion.signature),
            userHandle: assertion.userHandle ? bufferToBase64url(assertion.userHandle) : undefined,
        },
    };
}

async function safeJson(response: Response): Promise<{ error?: string; [key: string]: unknown }> {
    try {
        return await response.json();
    } catch {
        return {};
    }
}

function isCancellation(err: unknown): boolean {
    return err instanceof DOMException && err.name === "NotAllowedError";
}

// ---------------------------------------------------------------------------
// Registration (Settings > Security)
// ---------------------------------------------------------------------------

export interface RegisterConfig {
    optionsUrl: string;
    registerUrl: string;
    /** Optional nickname to save. Left blank, the server auto-generates one (e.g. "Passkey 2") - see webauthn.py. */
    name?: string;
}

export interface WebAuthnResult {
    ok: boolean;
    error?: string;
}

export async function registerPasskey(cfg: RegisterConfig): Promise<WebAuthnResult> {
    if (!window.PublicKeyCredential) {
        return { ok: false, error: "This browser doesn't support passkeys." };
    }
    try {
        const optionsResp = await fetch(cfg.optionsUrl, {
            method: "POST",
            headers: { "X-CSRFToken": csrfToken() },
            credentials: "same-origin",
        });
        if (!optionsResp.ok) {
            const body = await safeJson(optionsResp);
            return { ok: false, error: body.error ?? "Could not start passkey registration." };
        }
        const optionsJson = (await optionsResp.json()) as RegistrationOptionsJSON;
        const credential = (await navigator.credentials.create(creationOptionsFromJSON(optionsJson))) as PublicKeyCredential | null;
        if (!credential) {
            return { ok: false, error: "Passkey creation was cancelled." };
        }

        const name = cfg.name ?? "";
        const form = new URLSearchParams();
        form.set("credential", JSON.stringify(credentialToJSON(credential)));
        form.set("name", name);
        const completeResp = await fetch(cfg.registerUrl, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrfToken() },
            credentials: "same-origin",
            body: form.toString(),
        });
        const completeBody = await safeJson(completeResp);
        if (!completeResp.ok) {
            return { ok: false, error: completeBody.error ?? "That passkey could not be saved." };
        }
        return { ok: true };
    } catch (err) {
        return { ok: false, error: isCancellation(err) ? "Passkey creation was cancelled." : "Something went wrong creating that passkey." };
    }
}

// ---------------------------------------------------------------------------
// Login-time authentication (accounts/login/2fa/)
// ---------------------------------------------------------------------------

export interface LoginConfig {
    optionsUrl: string;
    verifyUrl: string;
    retryButtonId: string;
    statusElId: string;
}

export function runLogin(cfg: LoginConfig): void {
    const statusEl = document.getElementById(cfg.statusElId);
    const retryBtn = document.getElementById(cfg.retryButtonId) as HTMLButtonElement | null;

    function setStatus(text: string): void {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.hidden = !text;
    }

    async function attempt(): Promise<void> {
        if (!window.PublicKeyCredential) {
            setStatus("This browser doesn't support passkeys. Try a different device or browser.");
            return;
        }
        if (retryBtn) retryBtn.disabled = true;
        setStatus("");
        try {
            const optionsResp = await fetch(cfg.optionsUrl, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken() },
                credentials: "same-origin",
            });
            if (!optionsResp.ok) {
                const body = await safeJson(optionsResp);
                setStatus(body.error ?? "Could not start passkey sign-in.");
                return;
            }
            const optionsJson = (await optionsResp.json()) as AuthenticationOptionsJSON;
            const credential = (await navigator.credentials.get(requestOptionsFromJSON(optionsJson))) as PublicKeyCredential | null;
            if (!credential) {
                setStatus("Passkey sign-in was cancelled.");
                return;
            }
            const verifyResp = await fetch(cfg.verifyUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
                credentials: "same-origin",
                body: JSON.stringify(credentialToJSON(credential)),
            });
            const verifyBody = await safeJson(verifyResp);
            if (!verifyResp.ok) {
                setStatus((verifyBody.error as string | undefined) ?? "That passkey could not be verified.");
                return;
            }
            window.location.href = (verifyBody.redirect as string | undefined) || "/";
        } catch (err) {
            setStatus(isCancellation(err) ? "Passkey sign-in was cancelled." : "Something went wrong verifying that passkey.");
        } finally {
            if (retryBtn) retryBtn.disabled = false;
        }
    }

    retryBtn?.addEventListener("click", () => {
        void attempt();
    });
    void attempt();
}
