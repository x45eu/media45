from django.conf import settings
from django.db import models


class Media(models.Model):
    class Kind(models.TextChoices):
        MOVIE = "movie", "Film"
        SERIES = "series", "Serie"

    class State(models.TextChoices):
        REQUESTED = "requested", "Angefragt"
        AVAILABLE = "available", "Vorhanden"
        IN_PROGRESS = "in_progress", "In Bearbeitung"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=300)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    external_provider = models.CharField(max_length=32)
    external_id = models.CharField(max_length=128)
    state = models.CharField(max_length=16, choices=State.choices, default=State.REQUESTED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "external_provider", "external_id"],
                name="media_unique_external_identity",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.get_kind_display()})"



class MediaDetails(models.Model):
    """Optional TMDB metadata cached offline for one verified media identity."""

    media = models.OneToOneField(Media, on_delete=models.CASCADE, related_name="details")
    imdb_id = models.CharField(max_length=16)
    tmdb_id = models.PositiveIntegerField()
    synopsis = models.TextField(blank=True)
    genres = models.JSONField(default=list, blank=True)
    runtime_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    fetched_at = models.DateTimeField()

    def __str__(self):
        return f"TMDB: {self.media.title}"




class MediaRequest(models.Model):
    media = models.ForeignKey(Media, on_delete=models.PROTECT, related_name="requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    note = models.TextField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["media", "user"], name="unique_media_request_per_user")
        ]

    def __str__(self):
        return f"{self.user}: {self.media}"


class Notification(models.Model):
    title = models.CharField(max_length=150)
    body = models.TextField(max_length=1000)
    target_path = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)


class SearchAttempt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)


class ProviderThrottle(models.Model):
    provider = models.CharField(max_length=32, primary_key=True)
    last_call = models.DateTimeField()


class ScannedFile(models.Model):
    """Local inventory entry, deliberately separate from verified media identities."""

    class Root(models.TextChoices):
        MOVIES = "movies", "Filme"
        SERIES = "series", "Serien"

    root = models.CharField(max_length=12, choices=Root.choices)
    relative_path = models.TextField()
    media_kind = models.CharField(max_length=16, choices=Media.Kind.choices)
    suggested_title = models.CharField(max_length=300)
    file_size = models.BigIntegerField()
    modified_ns = models.BigIntegerField()
    last_seen_at = models.DateTimeField()
    group = models.ForeignKey(
        "MediaGroup", null=True, blank=True, related_name="files",
        on_delete=models.SET_NULL,
    )
    media = models.ForeignKey(
        Media, null=True, blank=True, related_name="scanned_files",
        on_delete=models.SET_NULL,
        help_text="Nur nach Prüfung mit einem eindeutig identifizierten Katalogeintrag verknüpfen.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["root", "relative_path"], name="unique_scanned_path")
        ]
        indexes = [models.Index(fields=["root", "last_seen_at"], name="scan_root_seen_idx")]

    def __str__(self):
        return f"{self.root}: {self.relative_path}"


class MediaGroup(models.Model):
    """Unverified grouping suggestion derived strictly from indexed file paths."""

    root = models.CharField(max_length=12, choices=ScannedFile.Root.choices)
    group_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Media.Kind.choices)
    suggested_title = models.CharField(max_length=300)
    file_count = models.PositiveIntegerField(default=0)
    review_deferred = models.BooleanField(default=False)
    metadata_checked_at = models.DateTimeField(null=True, blank=True)
    metadata_result_count = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["root", "group_key"], name="unique_media_group_root_key"
            ),
        ]
        ordering = ["root", "suggested_title"]

    def __str__(self):
        return f"{self.suggested_title} ({self.get_kind_display()})"


class MetadataCandidate(models.Model):
    """Unconfirmed match retrieved server-side from a public metadata provider."""

    group = models.ForeignKey(MediaGroup, on_delete=models.CASCADE, related_name="candidates")
    kind = models.CharField(max_length=16, choices=Media.Kind.choices)
    title = models.CharField(max_length=300)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    external_provider = models.CharField(max_length=32)
    external_id = models.CharField(max_length=128)
    manually_verified = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "kind", "external_provider", "external_id"],
                name="unique_metadata_candidate",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.external_provider}:{self.external_id})"


class TotpDevice(models.Model):
    """Per-account TOTP secret; do not expose in the Django admin or logs."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media45_totp")
    secret = models.CharField(max_length=64)
    enabled_at = models.DateTimeField(auto_now_add=True)
    last_counter = models.BigIntegerField(default=-1)


class TotpRecoveryCode(models.Model):
    device = models.ForeignKey(TotpDevice, on_delete=models.CASCADE, related_name="recovery_codes")
    digest = models.CharField(max_length=64, unique=True)
    used_at = models.DateTimeField(null=True, blank=True)


class TotpAttempt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    occurred_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["user", "occurred_at"], name="mfa_attempt_window")]


class LoginGuard(models.Model):
    """Sliding login-failure bucket; keyed with HMAC, not raw usernames/IPs."""
    key = models.CharField(max_length=64, primary_key=True)
    failures = models.PositiveSmallIntegerField(default=0)
    window_started = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True)


class PasskeyCredential(models.Model):
    """Verified WebAuthn public key. Never stores a private key."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media45_passkeys")
    name = models.CharField(max_length=80)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
