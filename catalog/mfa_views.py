"""MFA enrollment and verification. No third-party identity services."""
import secrets

import pyotp
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from . import mfa
from .models import TotpDevice, TotpRecoveryCode


@never_cache
@sensitive_post_parameters("password", "code")
@login_required
@require_http_methods(["GET", "POST"])
def setup(request):
    if mfa.has_device(request.user):
        return redirect("mfa_verify" if not mfa.is_verified(request) else "mfa_settings")
    pending = request.session.get(mfa.PENDING_SECRET)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "begin":
            if not request.user.check_password(request.POST.get("password", "")):
                messages.error(request, "Das aktuelle Passwort ist nicht korrekt.")
            else:
                pending = pyotp.random_base32()
                request.session[mfa.PENDING_SECRET] = pending
                return redirect("mfa_setup")
        elif action == "confirm" and pending:
            value = request.POST.get("code", "").strip()
            if pyotp.TOTP(pending).verify(value, valid_window=1):
                with transaction.atomic():
                    device, created = TotpDevice.objects.get_or_create(
                        user=request.user, defaults={"secret": pending}
                    )
                    if not created:
                        return redirect("mfa_verify")
                    now_counter = int(__import__("time").time()) // 30
                    device.last_counter = now_counter
                    device.save(update_fields=["last_counter"])
                    codes = mfa.make_recovery_codes(device)
                request.session.pop(mfa.PENDING_SECRET, None)
                request.session[mfa.ONCE_RECOVERY] = codes
                request.session[mfa.SESSION_MARKER] = request.user.pk
                return redirect("mfa_recovery")
            messages.error(request, "Ungültiger Authenticator-Code. Bitte erneut versuchen.")
        else:
            messages.error(request, "Bitte zuerst dein Passwort bestätigen.")
    uri = pyotp.TOTP(pending).provisioning_uri(
        name=request.user.get_username(), issuer_name="media45"
    ) if pending else None
    return render(request, "catalog/mfa_setup.html", {"pending": pending, "uri": uri})


@never_cache
@sensitive_post_parameters("code")
@login_required
@require_http_methods(["GET", "POST"])
def verify(request):
    if not mfa.has_device(request.user):
        return redirect("mfa_setup")
    if mfa.is_verified(request):
        return redirect("mfa_settings")
    if request.method == "POST":
        try:
            success = mfa.verify_second_factor(request.user, request.POST.get("code", ""))
        except ValidationError as exc:
            success = False
            messages.error(request, "; ".join(exc.messages))
        if success:
            request.session[mfa.SESSION_MARKER] = request.user.pk
            return redirect("index")
        if not success:
            messages.error(request, "Code ungültig oder bereits verwendet.")
    return render(request, "catalog/mfa_verify.html")


@never_cache
@login_required
def recovery(request):
    if not mfa.is_verified(request):
        return redirect("mfa_verify")
    codes = request.session.pop(mfa.ONCE_RECOVERY, None)
    if not codes:
        return redirect("index")
    return render(request, "catalog/mfa_recovery.html", {"codes": codes})


@never_cache
@sensitive_post_parameters("password", "code")
@login_required
@require_http_methods(["GET", "POST"])
def settings(request):
    """Second-factor account overview and explicitly reauthenticated code rotation."""
    if not mfa.has_device(request.user):
        return redirect("mfa_setup")
    if not mfa.is_verified(request):
        return redirect("mfa_verify")

    if request.method == "POST":
        password = request.POST.get("password", "")
        otp = request.POST.get("code", "").strip()
        if not request.user.check_password(password):
            messages.error(request, "Das aktuelle Passwort ist nicht korrekt.")
            return redirect("mfa_settings")
        if len(otp) != 6 or not otp.isascii() or not otp.isdecimal():
            messages.error(request, "Bitte einen aktuellen sechsstelligen Authenticator-Code eingeben.")
            return redirect("mfa_settings")
        try:
            if not mfa.verify_second_factor(request.user, otp):
                messages.error(request, "Authenticator-Code ungültig oder bereits verwendet.")
                return redirect("mfa_settings")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("mfa_settings")
        with transaction.atomic():
            device = TotpDevice.objects.select_for_update().get(user=request.user)
            TotpRecoveryCode.objects.filter(device=device).delete()
            codes = mfa.make_recovery_codes(device)
        request.session[mfa.ONCE_RECOVERY] = codes
        messages.success(request, "Neue Notfallcodes erstellt. Vorherige Codes sind ab sofort ungültig.")
        return redirect("mfa_recovery")

    device = TotpDevice.objects.get(user=request.user)
    remaining = device.recovery_codes.filter(used_at__isnull=True).count()
    return render(request, "catalog/mfa_settings.html", {
        "device": device, "recovery_remaining": remaining,
        "mfa_required": request.user.is_staff or request.user.is_superuser,
    })
