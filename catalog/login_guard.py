"""PostgreSQL-backed password login throttling (including Django admin).

Only REMOTE_ADDR is used. Do not trust spoofable X-Forwarded-For headers.
Neither raw usernames nor IP addresses are stored or logged by this module.
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import LoginGuard

WINDOW = timedelta(minutes=15)
PAIR_LIMIT = 8
ADDRESS_LIMIT = 60


def guard_keys(request, username):
    address = request.META.get("REMOTE_ADDR", "unknown")[:100]
    normalized = (username or "").strip().casefold()[:150]
    secret = settings.SECRET_KEY.encode("utf-8")
    def hashed(value):
        return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        hashed("pair:" + address + "\\0" + normalized),
        hashed("source:" + address),
    )


def blocked(request, username):
    now = timezone.now()
    keys = guard_keys(request, username)
    for bucket in LoginGuard.objects.filter(key__in=keys):
        if bucket.blocked_until and bucket.blocked_until > now:
            return True
        if bucket.window_started >= now - WINDOW:
            ceiling = PAIR_LIMIT if bucket.key == keys[0] else ADDRESS_LIMIT
            if bucket.failures >= ceiling:
                return True
    return False


def record_failure(request, username):
    now = timezone.now()
    keys = guard_keys(request, username)
    for key, limit in ((keys[0], PAIR_LIMIT), (keys[1], ADDRESS_LIMIT)):
        with transaction.atomic():
            bucket, _ = LoginGuard.objects.get_or_create(
                key=key, defaults={"window_started": now}
            )
            bucket = LoginGuard.objects.select_for_update().get(pk=bucket.pk)
            if bucket.window_started < now - WINDOW:
                bucket.window_started = now
                bucket.failures = 0
                bucket.blocked_until = None
            bucket.failures = min(bucket.failures + 1, limit)
            if bucket.failures >= limit:
                bucket.blocked_until = now + WINDOW
            bucket.save(update_fields=["window_started", "failures", "blocked_until"])


def record_success(request, username):
    """Clear only this credential pair, never the shared source budget."""
    LoginGuard.objects.filter(key=guard_keys(request, username)[0]).delete()


class LoginThrottleMixin:
    def clean(self):
        username = self.cleaned_data.get("username") or ""
        password = self.cleaned_data.get("password")
        # Django's standard required-field validation runs before clean().
        if username and password is not None:
            if blocked(self.request, username):
                raise ValidationError(
                    "Zu viele Anmeldeversuche. Bitte in 15 Minuten erneut versuchen.",
                    code="rate_limited",
                )
            try:
                result = super().clean()
            except ValidationError:
                record_failure(self.request, username)
                raise
            record_success(self.request, username)
            return result
        return super().clean()


class ProtectedAuthenticationForm(LoginThrottleMixin, AuthenticationForm):
    pass


class ProtectedAdminAuthenticationForm(LoginThrottleMixin, AdminAuthenticationForm):
    pass
