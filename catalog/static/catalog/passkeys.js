/* Native WebAuthn only, no third-party SDK or analytics. */
"use strict";
(function () {
  const form = document.getElementById("passkey-register") || document.getElementById("passkey-auth") || document.getElementById("passkey-login");
  if (!form) return;
  const status = document.getElementById("passkey-status");
  const button = form.querySelector("button");
  const bytes = (input) => {
    const s = input.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(s.padEnd(Math.ceil(s.length / 4) * 4, "="));
    return Uint8Array.from(binary, c => c.charCodeAt(0));
  };
  const b64 = (input) => {
    const data = new Uint8Array(input);
    let str = "";
    for (const b of data) str += String.fromCharCode(b);
    return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const csrf = form.querySelector("input[name=csrfmiddlewaretoken]").value;
  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, "Accept": "application/json" },
      body: body,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "WebAuthn-Anfrage fehlgeschlagen.");
    return result;
  }
  const encode = (credential, register) => ({
    id: credential.id,
    rawId: b64(credential.rawId),
    type: credential.type,
    response: register ? {
      attestationObject: b64(credential.response.attestationObject),
      clientDataJSON: b64(credential.response.clientDataJSON),
      transports: credential.response.getTransports?.() || [],
    } : {
      authenticatorData: b64(credential.response.authenticatorData),
      clientDataJSON: b64(credential.response.clientDataJSON),
      signature: b64(credential.response.signature),
      userHandle: credential.response.userHandle ? b64(credential.response.userHandle) : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment,
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!window.PublicKeyCredential || !navigator.credentials) {
      status.textContent = "Dieser Browser unterstützt WebAuthn nicht.";
      return;
    }
    button.disabled = true;
    status.textContent = "Sicherheitsschlüssel wird angefordert …";
    const register = form.id === "passkey-register";
    try {
      const body = (register || form.id === "passkey-login") ? new FormData(form) : new FormData();
      const options = await post(form.dataset.options, body);
      options.challenge = bytes(options.challenge);
      if (register) {
        options.user.id = bytes(options.user.id);
        options.excludeCredentials = (options.excludeCredentials || []).map(item => ({
          ...item, id: bytes(item.id),
        }));
      } else {
        options.allowCredentials = (options.allowCredentials || []).map(item => ({
          ...item, id: bytes(item.id),
        }));
      }
      const credential = register
        ? await navigator.credentials.create({ publicKey: options })
        : await navigator.credentials.get({ publicKey: options });
      if (!credential) throw new Error("Keine Sicherheitsbestätigung erhalten.");
      const result = await post(form.dataset.finish, JSON.stringify(encode(credential, register)));
      status.textContent = "Sicherheitsschlüssel bestätigt.";
      window.location.assign(register ? window.location.pathname : (result.redirect || "/"));
    } catch (error) {
      status.textContent = error.name === "NotAllowedError"
        ? "Abgebrochen oder Sicherheitsschlüssel nicht verfügbar."
        : (error.message || "Der Vorgang konnte nicht abgeschlossen werden.");
    } finally {
      button.disabled = false;
    }
  });
})();
