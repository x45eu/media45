"""Passkey enrollment, management and WebAuthn assertion as MFA."""
import json

import webauthn
from webauthn.helpers.exceptions import WebAuthnException
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model, login as django_login
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.debug import sensitive_post_parameters

from . import mfa, passkeys
from . import login_guard
from .models import PasskeyCredential


def error(message, status=400):
    return JsonResponse({"error": message}, status=status)


@login_required
@never_cache
@require_GET
def manage(request):
    if mfa.must_verify(request.user) and not mfa.is_verified(request):
        return redirect("mfa_verify" if mfa.has_device(request.user) else "mfa_setup")
    return render(request, "catalog/passkeys.html", {
        "credentials": PasskeyCredential.objects.filter(user=request.user).order_by("created_at"),
    })


@login_required
@never_cache
@csrf_protect
@require_POST
@sensitive_post_parameters("password")
def register_options(request):
    if mfa.must_verify(request.user) and not mfa.is_verified(request):
        return error("Zuerst die Zwei-Faktor-Anmeldung bestätigen.", status=403)
    if not request.user.check_password(request.POST.get("password", "")):
        return error("Das aktuelle Passwort ist nicht korrekt.", status=403)
    name = request.POST.get("name", "").strip()
    if not 1 <= len(name) <= 80:
        return error("Bitte einen Namen zwischen 1 und 80 Zeichen eingeben.")
    credentials = list(PasskeyCredential.objects.filter(user=request.user))
    try:
        options = passkeys.registration_options(request, credentials)
    except (ValidationError, ValueError) as exc:
        return error(str(exc))
    request.session["media45_passkey_pending_name"] = name
    return JsonResponse(options)


@login_required
@never_cache
@csrf_protect
@require_POST
def register_finish(request):
    if mfa.must_verify(request.user) and not mfa.is_verified(request):
        return error("Zuerst die Zwei-Faktor-Anmeldung bestätigen.", status=403)
    try:
        challenge = passkeys.consume_challenge(request, passkeys.REGISTER_SESSION)
        name = request.session.pop("media45_passkey_pending_name", None)
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ValidationError("Registrierung abgelaufen.")
        payload = passkeys.credential_payload(request)
        verified = webauthn.verify_registration_response(
            credential=payload,
            expected_challenge=challenge,
            expected_rp_id=passkeys.rp_id(),
            expected_origin=settings_origin(),
            require_user_verification=True,
        )
        with transaction.atomic():
            if PasskeyCredential.objects.select_for_update().filter(user=request.user).count() >= passkeys.MAX_PASSKEYS:
                raise ValidationError("Maximal zehn Sicherheitsschlüssel erlaubt.")
            PasskeyCredential.objects.create(
                user=request.user,
                name=name,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
            )
    except (ValidationError, ValueError, TypeError, IntegrityError, KeyError, WebAuthnException):
        return error("Registrierung fehlgeschlagen oder bereits vorhanden.")
    return JsonResponse({"ok": True})


def settings_origin():
    from django.conf import settings
    return settings.ORIGIN.rstrip("/")


@login_required
@never_cache
@csrf_protect
@require_POST
def auth_options(request):
    if not mfa.has_device(request.user) or mfa.is_verified(request):
        return error("Keine zusätzliche Authentifizierung erforderlich.", status=403)
    credentials = list(PasskeyCredential.objects.filter(user=request.user))
    try:
        return JsonResponse(passkeys.assertion_options(request, credentials))
    except (ValidationError, ValueError):
        return error("Kein Sicherheitsschlüssel verfügbar.")


@login_required
@never_cache
@csrf_protect
@require_POST
def auth_finish(request):
    if not mfa.has_device(request.user) or mfa.is_verified(request):
        return error("Keine zusätzliche Authentifizierung erforderlich.", status=403)
    try:
        challenge = passkeys.consume_challenge(request, passkeys.AUTH_SESSION)
        payload = passkeys.credential_payload(request)
        raw_id = passkeys.unb64(payload["id"])
        with transaction.atomic():
            device = PasskeyCredential.objects.select_for_update().get(user=request.user, credential_id=raw_id)
            verified = webauthn.verify_authentication_response(
                credential=payload,
                expected_challenge=challenge,
                expected_rp_id=passkeys.rp_id(),
                expected_origin=settings_origin(),
                credential_public_key=bytes(device.public_key),
                credential_current_sign_count=device.sign_count,
                require_user_verification=True,
            )
            device.sign_count = verified.new_sign_count
            device.last_used_at = timezone.now()
            device.save(update_fields=["sign_count", "last_used_at"])
        request.session[mfa.SESSION_MARKER] = request.user.pk
    except (ValidationError, ValueError, TypeError, KeyError, PasskeyCredential.DoesNotExist, WebAuthnException):
        return error("Sicherheitsschlüssel konnte nicht bestätigt werden.")
    return JsonResponse({"ok": True, "redirect": "/"})


@login_required
@never_cache
@csrf_protect
@require_POST
@sensitive_post_parameters("password", "code")
def remove(request, pk):
    if mfa.must_verify(request.user) and not mfa.is_verified(request):
        return redirect("mfa_verify")
    credential = get_object_or_404(PasskeyCredential, user=request.user, pk=pk)
    if not request.user.check_password(request.POST.get("password", "")):
        return redirect("passkeys_manage")
    if mfa.has_device(request.user):
        try:
            if not mfa.verify_second_factor(request.user, request.POST.get("code", "")):
                return redirect("passkeys_manage")
        except ValidationError:
            return redirect("passkeys_manage")
    credential.delete()
    return redirect("passkeys_manage")


@never_cache
@csrf_protect
@require_POST
@sensitive_post_parameters("username")
def passwordless_options(request):
    """Start a passwordless login after entering a username (no password)."""
    # Never allow an old or partially completed ceremony to cross account boundaries.
    request.session.pop(passkeys.LOGIN_SESSION, None)
    username = (request.POST.get("username") or "").strip()
    if len(username) > 150 or not username:
        return error("Anmeldung nicht möglich.")
    if login_guard.blocked(request, username):
        return error("Zu viele Versuche. Bitte in 15 Minuten erneut versuchen.", status=429)
    user = get_user_model()._default_manager.filter(username=username, is_active=True).first()
    if not user:
        login_guard.record_failure(request, username)
        return error("Anmeldung nicht möglich.")
    credentials = list(PasskeyCredential.objects.filter(user=user))
    if not credentials:
        login_guard.record_failure(request, username)
        return error("Anmeldung nicht möglich.")
    try:
        options = passkeys.login_options(request, user, credentials)
    except (ValidationError, ValueError):
        return error("Anmeldung nicht möglich.")
    return JsonResponse(options)


@never_cache
@csrf_protect
@require_POST
def passwordless_finish(request):
    """Verify UV+origin+RP+signature+counter before creating Django session."""
    try:
        user_pk, challenge = passkeys.consume_login_challenge(request)
        user = get_user_model()._default_manager.get(pk=user_pk, is_active=True)
        username = user.get_username()
        if login_guard.blocked(request, username):
            return error("Zu viele Versuche. Bitte in 15 Minuten erneut versuchen.", status=429)
        payload = passkeys.credential_payload(request)
        raw_id = passkeys.unb64(payload["id"])
        # Optional userHandle is verified when supplied by the authenticator.
        handle = payload.get("response", {}).get("userHandle")
        if handle is not None and passkeys.unb64(handle) != str(user.pk).encode("utf-8"):
            raise ValidationError("Passkey gehört nicht zu diesem Konto.")
        with transaction.atomic():
            device = PasskeyCredential.objects.select_for_update().get(
                user=user, credential_id=raw_id
            )
            verified = webauthn.verify_authentication_response(
                credential=payload,
                expected_challenge=challenge,
                expected_rp_id=passkeys.rp_id(),
                expected_origin=settings_origin(),
                credential_public_key=bytes(device.public_key),
                credential_current_sign_count=device.sign_count,
                require_user_verification=True,
            )
            device.sign_count = verified.new_sign_count
            device.last_used_at = timezone.now()
            device.save(update_fields=["sign_count", "last_used_at"])
        django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session[mfa.SESSION_MARKER] = user.pk
        login_guard.record_success(request, username)
        return JsonResponse({"ok": True, "redirect": "/"})
    except (ValidationError, ValueError, TypeError, KeyError,
            PasskeyCredential.DoesNotExist, get_user_model().DoesNotExist,
            WebAuthnException):
        # Failed ceremonies are counted without revealing account/credential existence.
        if "username" in locals():
            login_guard.record_failure(request, username)
        return error("Passkey-Anmeldung nicht möglich. Bitte erneut versuchen.")
