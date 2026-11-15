"""Run repeatedly as a worker. No secrets appear in logs or messages."""
import logging
import os
import time
from datetime import timedelta

import requests
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.models import Notification

log = logging.getLogger(__name__)


def send_pending():
    url = os.getenv("GOTIFY_URL", "").rstrip("/")
    token = os.getenv("GOTIFY_TOKEN", "")
    origin = os.getenv("MEDIA45_ORIGIN", "").rstrip("/")
    if not url or not token:
        return
    if not url.startswith("https://") or not origin.startswith("https://"):
        log.error("Gotify requires HTTPS URL and HTTPS MEDIA45_ORIGIN")
        return
    pending = Notification.objects.filter(
        delivered_at__isnull=True, next_attempt__lte=timezone.now(), attempts__lt=12,
    ).order_by("created_at").values_list("pk", flat=True)[:20]
    for pk in pending:
        with transaction.atomic():
            notification = Notification.objects.select_for_update(skip_locked=True).filter(
                pk=pk, delivered_at__isnull=True, next_attempt__lte=timezone.now()
            ).first()
            if notification is None:
                continue
            payload = {
                "title": notification.title,
                "message": notification.body,
                "priority": 5,
                "extras": {"client::notification": {
                    "click": {"url": origin + notification.target_path}
                }},
            }
            try:
                response = requests.post(
                    url + "/message",
                    json=payload,
                    headers={"X-Gotify-Key": token},
                    timeout=(3, 8),
                )
                response.raise_for_status()
                notification.delivered_at = timezone.now()
            except requests.RequestException:
                log.warning("Gotify delivery failed for notification %s", pk)
            notification.attempts += 1
            notification.next_attempt = timezone.now() + timedelta(
                minutes=min(2 ** notification.attempts, 120)
            )
            notification.save(update_fields=[
                "delivered_at", "attempts", "next_attempt"
            ])


class Command(BaseCommand):
    help = "Deliver queued Gotify messages, optionally in worker loop"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, **options):
        if options["loop"]:
            while True:
                send_pending()
                time.sleep(15)
        else:
            send_pending()
