"""Local TOTP second-factor enforcement. Secrets never leave media45."""
import hashlib
import hmac
import secrets
import time
from datetime import timedelta

import pyotp
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .models import TotpAttempt, TotpDevice, TotpRecoveryCode

SESSION_MARKER = "media45_mfa_verified_user"
PENDING_SECRET = "media45_mfa_pending"
ONCE_RECOVERY = "media45_mfa_recovery_once"


def has_device(user):
    return TotpDevice.objects.filter(user=user).exists()


def must_verify(user):
    return user.is_authenticated and user.is_active and (user.is_staff or user.is_superuser or has_device(user))


def is_verified(request):
    return (
        request.user.is_authenticated
        and request.session.get(SESSION_MARKER) == request.user.pk
        and has_device(request.user)
    )


def valid_destination(request, raw):
    if raw and raw.startswith("/") and not raw.startswith("//") and url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=False
    ):
        return raw
    return "/"


def make_recovery_codes(device):
    codes = [secrets.token_hex(12).upper() for _ in range(10)]
    TotpRecoveryCode.objects.bulk_create([
        TotpRecoveryCode(device=device, digest=hashlib.sha256(code.encode("ascii")).hexdigest())
        for code in codes
    ])
    return codes


def verify_second_factor(user, raw):
    """Verify within row lock; each TOTP timestep or recovery code works once."""
    code = raw.strip().replace(" ", "").replace("-", "").upper()
    if not 4 <= len(code) <= 64:
        return False
    with transaction.atomic():
        device = TotpDevice.objects.select_for_update().filter(user=user).first()
        if not device:
            return False
        # DB-backed per-account failure window, effective across sessions and workers.
        start = timezone.now() - timedelta(minutes=5)
        if TotpAttempt.objects.filter(user=user, success=False, occurred_at__gte=start).count() >= 6:
            raise ValidationError("Zu viele Fehlversuche. Bitte in fünf Minuten erneut versuchen.")
        ok = False
        if len(code) == 6 and code.isdigit():
            counter = int(time.time()) // 30
            totp = pyotp.TOTP(device.secret)
            for candidate_counter in (counter - 1, counter, counter + 1):
                if candidate_counter <= device.last_counter:
                    continue
                expected = totp.at(candidate_counter * 30)
                if hmac.compare_digest(expected, code):
                    device.last_counter = candidate_counter
                    device.save(update_fields=["last_counter"])
                    ok = True
                    break
        elif len(code) == 24 and all(c in "0123456789ABCDEF" for c in code):
            digest = hashlib.sha256(code.encode("ascii")).hexdigest()
            recovery = TotpRecoveryCode.objects.select_for_update().filter(
                device=device, digest=digest, used_at__isnull=True
            ).first()
            if recovery:
                recovery.used_at = timezone.now()
                recovery.save(update_fields=["used_at"])
                ok = True
        TotpAttempt.objects.create(user=user, success=ok)
        if ok:
            # Successful second-factor resets the failure budget.
            TotpAttempt.objects.filter(user=user, success=False).delete()
        return ok


class MfaGateMiddleware:
    """Protect staff privileges and opted-in users, even on old sessions."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if must_verify(request.user) and not is_verified(request):
            path = request.path_info
            allowed = ("/account/mfa/", "/account/mfa/setup/", "/account/mfa/settings/", "/account/mfa/recovery/",
                       "/account/passkeys/auth/options/", "/account/passkeys/auth/finish/",
                       "/login/", "/logout/", "/static/", "/manifest.webmanifest", "/service-worker.js")
            if not any(path == prefix or (prefix == "/static/" and path.startswith(prefix)) for prefix in allowed):
                target = "/account/mfa/" if has_device(request.user) else "/account/mfa/setup/"
                return redirect(target)
        return self.get_response(request)
