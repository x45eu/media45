"""WebAuthn second-factor ceremonies bound to the logged-in Django session.

Registration: existing MFA (if enabled) and current password required.
Assertion: alternatives to TOTP *after* a successful password login.
Passwordless login uses a separate ceremony and session challenge.
"""
import base64
import json
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

REGISTER_SESSION = "media45_passkey_registration"
AUTH_SESSION = "media45_passkey_assertion"
LOGIN_SESSION = "media45_passkey_login"
CHALLENGE_TTL = 120
MAX_PASSKEYS = 10
MAX_PAYLOAD = 16384


def rp_id():
    parsed = urlsplit(settings.ORIGIN)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("WebAuthn requires a fixed HTTPS MEDIA45_ORIGIN.")
    return parsed.hostname


def b64(data):
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def unb64(value):
    return webauthn.base64url_to_bytes(value)


def credential_payload(request):
    if len(request.body) > MAX_PAYLOAD:
        raise ValidationError("WebAuthn-Antwort zu groß.")
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Ungültige WebAuthn-Antwort.") from exc


def store_challenge(request, key, challenge):
    request.session[key] = {
        "user": request.user.pk,
        "challenge": b64(challenge),
        "created": int(time.time()),
    }


def consume_challenge(request, key):
    state = request.session.pop(key, None)
    if not isinstance(state, dict) or state.get("user") != request.user.pk:
        raise ValidationError("Sicherheitsschlüssel-Anfrage ungültig oder abgelaufen.")
    if not isinstance(state.get("created"), int) or not 0 <= time.time() - state["created"] <= CHALLENGE_TTL:
        raise ValidationError("Sicherheitsschlüssel-Anfrage ist abgelaufen.")
    try:
        return unb64(state["challenge"])
    except Exception as exc:
        raise ValidationError("Sicherheitsschlüssel-Anfrage ungültig.") from exc


def registration_options(request, credentials):
    if len(credentials) >= MAX_PASSKEYS:
        raise ValidationError("Es können höchstens zehn Sicherheitsschlüssel hinterlegt werden.")
    options = webauthn.generate_registration_options(
        rp_id=rp_id(),
        rp_name="media45",
        user_id=str(request.user.pk).encode("utf-8"),
        user_name=request.user.get_username(),
        user_display_name=request.user.get_username(),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(item.credential_id))
            for item in credentials
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    store_challenge(request, REGISTER_SESSION, options.challenge)
    return json.loads(webauthn.options_to_json(options))


def assertion_options(request, credentials):
    if not credentials:
        raise ValidationError("Für dieses Konto ist kein Sicherheitsschlüssel registriert.")
    options = webauthn.generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(item.credential_id))
            for item in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    store_challenge(request, AUTH_SESSION, options.challenge)
    return json.loads(webauthn.options_to_json(options))


def login_options(request, user, credentials):
    """Passwordless username-first WebAuthn. Also supports non-discoverable YubiKeys."""
    if not credentials:
        raise ValidationError("Keine Sicherheitsschlüssel vorhanden.")
    options = webauthn.generate_authentication_options(
        rp_id=rp_id(),
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(item.credential_id))
            for item in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session[LOGIN_SESSION] = {
        "user": user.pk, "challenge": b64(options.challenge),
        "created": int(time.time()),
    }
    return json.loads(webauthn.options_to_json(options))


def consume_login_challenge(request):
    """Single use, expiry checked before any login; unrelated to MFA ceremonies."""
    state = request.session.pop(LOGIN_SESSION, None)
    if not isinstance(state, dict) or not isinstance(state.get("user"), int):
        raise ValidationError("Passkey-Anmeldung ungültig oder abgelaufen.")
    if not isinstance(state.get("created"), int) or not 0 <= time.time() - state["created"] <= CHALLENGE_TTL:
        raise ValidationError("Passkey-Anmeldung abgelaufen.")
    try:
        return state["user"], unb64(state["challenge"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationError("Passkey-Anmeldung ungültig.") from exc
