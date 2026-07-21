(() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  function __accessProp(key) {
    return this[key];
  }
  var __toCommonJS = (from) => {
    var entry = (__moduleCache ??= new WeakMap).get(from), desc;
    if (entry)
      return entry;
    entry = __defProp({}, "__esModule", { value: true });
    if (from && typeof from === "object" || typeof from === "function") {
      for (var key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(entry, key))
          __defProp(entry, key, {
            get: __accessProp.bind(from, key),
            enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
          });
    }
    __moduleCache.set(from, entry);
    return entry;
  };
  var __moduleCache;
  var __commonJS = (cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports);
  var __returnValue = (v) => v;
  function __exportSetter(name, newValue) {
    this[name] = __returnValue.bind(null, newValue);
  }
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, {
        get: all[name],
        enumerable: true,
        configurable: true,
        set: __exportSetter.bind(all, name)
      });
  };
  var __esm = (fn, res) => () => (fn && (res = fn(fn = 0)), res);
  var __require = /* @__PURE__ */ ((x) => typeof require !== "undefined" ? require : typeof Proxy !== "undefined" ? new Proxy(x, {
    get: (a, b) => (typeof require !== "undefined" ? require : a)[b]
  }) : x)(function(x) {
    if (typeof require !== "undefined")
      return require.apply(this, arguments);
    throw Error('Dynamic require of "' + x + '" is not supported');
  });

  // src/urbanlens/dashboard/frontend/ts/shared/webauthn-client.ts
  function base64urlToBuffer(value) {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/");
    const padding = "=".repeat((4 - padded.length % 4) % 4);
    const raw = atob(padded + padding);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0;i < raw.length; i++) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes.buffer;
  }
  function bufferToBase64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0;i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function csrfToken() {
    return window.csrftoken ?? "";
  }
  function creationOptionsFromJSON(json) {
    return {
      publicKey: {
        rp: json.rp,
        user: {
          id: base64urlToBuffer(json.user.id),
          name: json.user.name,
          displayName: json.user.displayName
        },
        challenge: base64urlToBuffer(json.challenge),
        pubKeyCredParams: json.pubKeyCredParams,
        timeout: json.timeout,
        excludeCredentials: (json.excludeCredentials ?? []).map((cred) => ({
          id: base64urlToBuffer(cred.id),
          type: "public-key",
          transports: cred.transports
        })),
        authenticatorSelection: json.authenticatorSelection,
        attestation: json.attestation
      }
    };
  }
  function requestOptionsFromJSON(json) {
    return {
      publicKey: {
        challenge: base64urlToBuffer(json.challenge),
        timeout: json.timeout,
        rpId: json.rpId,
        allowCredentials: (json.allowCredentials ?? []).map((cred) => ({
          id: base64urlToBuffer(cred.id),
          type: "public-key",
          transports: cred.transports
        })),
        userVerification: json.userVerification
      }
    };
  }
  function credentialToJSON(credential) {
    const base = {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment ?? undefined
    };
    const response = credential.response;
    if (response instanceof AuthenticatorAttestationResponse) {
      return {
        ...base,
        response: {
          clientDataJSON: bufferToBase64url(response.clientDataJSON),
          attestationObject: bufferToBase64url(response.attestationObject),
          transports: response.getTransports ? response.getTransports() : undefined
        }
      };
    }
    const assertion = response;
    return {
      ...base,
      response: {
        clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
        authenticatorData: bufferToBase64url(assertion.authenticatorData),
        signature: bufferToBase64url(assertion.signature),
        userHandle: assertion.userHandle ? bufferToBase64url(assertion.userHandle) : undefined
      }
    };
  }
  async function safeJson(response) {
    try {
      return await response.json();
    } catch {
      return {};
    }
  }
  function isCancellation(err) {
    return err instanceof DOMException && err.name === "NotAllowedError";
  }
  async function registerPasskey(cfg) {
    if (!window.PublicKeyCredential) {
      return { ok: false, error: "This browser doesn't support passkeys." };
    }
    try {
      const optionsResp = await fetch(cfg.optionsUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken() },
        credentials: "same-origin"
      });
      if (!optionsResp.ok) {
        const body = await safeJson(optionsResp);
        return { ok: false, error: body.error ?? "Could not start passkey registration." };
      }
      const optionsJson = await optionsResp.json();
      const credential = await navigator.credentials.create(creationOptionsFromJSON(optionsJson));
      if (!credential) {
        return { ok: false, error: "Passkey creation was cancelled." };
      }
      const name = cfg.name ?? "";
      const form = new URLSearchParams;
      form.set("credential", JSON.stringify(credentialToJSON(credential)));
      form.set("name", name);
      const completeResp = await fetch(cfg.registerUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrfToken() },
        credentials: "same-origin",
        body: form.toString()
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
  function runLogin(cfg) {
    const statusEl = document.getElementById(cfg.statusElId);
    const retryBtn = document.getElementById(cfg.retryButtonId);
    function setStatus(text) {
      if (!statusEl)
        return;
      statusEl.textContent = text;
      statusEl.hidden = !text;
    }
    async function attempt() {
      if (!window.PublicKeyCredential) {
        setStatus("This browser doesn't support passkeys. Try a different device or browser.");
        return;
      }
      if (retryBtn)
        retryBtn.disabled = true;
      setStatus("");
      try {
        const optionsResp = await fetch(cfg.optionsUrl, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken() },
          credentials: "same-origin"
        });
        if (!optionsResp.ok) {
          const body = await safeJson(optionsResp);
          setStatus(body.error ?? "Could not start passkey sign-in.");
          return;
        }
        const optionsJson = await optionsResp.json();
        const credential = await navigator.credentials.get(requestOptionsFromJSON(optionsJson));
        if (!credential) {
          setStatus("Passkey sign-in was cancelled.");
          return;
        }
        const verifyResp = await fetch(cfg.verifyUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
          credentials: "same-origin",
          body: JSON.stringify(credentialToJSON(credential))
        });
        const verifyBody = await safeJson(verifyResp);
        if (!verifyResp.ok) {
          setStatus(verifyBody.error ?? "That passkey could not be verified.");
          return;
        }
        window.location.href = verifyBody.redirect || "/";
      } catch (err) {
        setStatus(isCancellation(err) ? "Passkey sign-in was cancelled." : "Something went wrong verifying that passkey.");
      } finally {
        if (retryBtn)
          retryBtn.disabled = false;
      }
    }
    retryBtn?.addEventListener("click", () => {
      attempt();
    });
    attempt();
  }

  // src/urbanlens/dashboard/frontend/ts/entries-classic/webauthn.ts
  var api = { registerPasskey, runLogin };
  window.UrbanLensWebAuthn = api;
})();
