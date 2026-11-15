"""Minimal, privacy-safe markers of scheduler errors.

Only fixed event names and timestamps are stored; no exception strings, paths,
media identities, credentials, user data or network payloads.
"""
from django.utils import timezone
from .models import ProviderThrottle

SCAN_FAILURE = "media45_scan_failure"
MATCH_FAILURE = "media45_match_failure"
DETAILS_FAILURE = "media45_details_failure"
SCHEDULER_HEARTBEAT = "media45_scheduler_beat"
DETAILS_HEARTBEAT = "media45_details_beat"


def record_heartbeat(key):
    """Record only a fixed service identifier and current time; no secrets."""
    if key not in (SCHEDULER_HEARTBEAT, DETAILS_HEARTBEAT):
        raise ValueError("Invalid heartbeat marker")
    ProviderThrottle.objects.update_or_create(
        provider=key, defaults={"last_call": timezone.now()}
    )

def record_failure(key):
    if key not in (SCAN_FAILURE, MATCH_FAILURE, DETAILS_FAILURE):
        raise ValueError("Invalid health marker")
    ProviderThrottle.objects.update_or_create(
        provider=key, defaults={"last_call": timezone.now()}
    )


def clear_failure(key):
    if key not in (SCAN_FAILURE, MATCH_FAILURE, DETAILS_FAILURE):
        raise ValueError("Invalid health marker")
    ProviderThrottle.objects.filter(provider=key).delete()
