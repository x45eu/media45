from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from .models import Media, MediaRequest, Notification, ScannedFile, MediaGroup, MetadataCandidate


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "year", "state", "external_provider", "external_id")
    list_filter = ("kind", "state")
    search_fields = ("title", "external_id")


@admin.register(MediaRequest)
class MediaRequestAdmin(admin.ModelAdmin):
    list_display = ("media", "user", "created_at")
    search_fields = ("media__title", "user__username")

    def delete_model(self, request, obj):
        from .request_cleanup import remove_requests_and_unused_media
        remove_requests_and_unused_media([obj.pk])

    def delete_queryset(self, request, queryset):
        from .request_cleanup import remove_requests_and_unused_media
        remove_requests_and_unused_media(queryset.values_list("pk", flat=True))


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "delivered_at", "attempts", "created_at")
    readonly_fields = ("title", "body", "target_path", "created_at", "delivered_at", "attempts", "next_attempt")

    def has_add_permission(self, request):
        return False


@admin.register(ScannedFile)
class ScannedFileAdmin(admin.ModelAdmin):
    list_display = ("suggested_title", "root", "group", "media_kind", "linked", "file_size", "last_seen_at")
    list_filter = ("root", "media_kind", "last_seen_at")
    search_fields = ("suggested_title", "relative_path", "media__title")
    readonly_fields = ("root", "relative_path", "group", "media_kind", "suggested_title", "file_size", "modified_ns", "last_seen_at")
    list_select_related = ("media",)
    list_per_page = 50

    @admin.display(boolean=True, description="Zugeordnet")
    def linked(self, obj):
        return obj.media_id is not None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("-last_seen_at")

    def save_model(self, request, obj, form, change):
        if obj.media_id and obj.media.kind != obj.media_kind:
            from django.core.exceptions import ValidationError
            raise ValidationError("Medientyp stimmt nicht mit dem verknüpften Katalogeintrag überein.")
        super().save_model(request, obj, form, change)


# No admin exposure of throttle/search telemetry.


@admin.register(MediaGroup)
class MediaGroupAdmin(admin.ModelAdmin):
    list_display = ("suggested_title", "kind", "root", "file_count", "metadata_result_count", "metadata_checked_at", "updated_at")
    list_filter = ("root", "kind")
    search_fields = ("suggested_title",)
    readonly_fields = ("root", "group_key", "kind", "suggested_title", "file_count", "metadata_result_count", "metadata_checked_at", "updated_at")
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MetadataCandidate)
class MetadataCandidateAdmin(admin.ModelAdmin):
    list_display = ("title", "group", "kind", "year", "external_provider", "external_id", "manually_verified", "fetched_at")
    list_filter = ("kind", "external_provider")
    search_fields = ("title", "group__suggested_title", "external_id")
    readonly_fields = ("group", "kind", "title", "year", "external_provider", "external_id", "manually_verified", "fetched_at")
    actions = ["confirm_matches"]
    list_select_related = ("group",)
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_approve_permission(self, request):
        # Approval changes the catalog and must not be available to view-only staff.
        return request.user.is_active and request.user.is_superuser

    @admin.action(
        permissions=["approve"],
        description="Ausgewählte Treffer bestätigen und als vorhanden markieren (max. 100)",
    )
    def confirm_matches(self, request, queryset):
        from catalog.matching import confirm_candidates

        try:
            approved = confirm_candidates(queryset.values_list("pk", flat=True))
        except ValidationError as exc:
            self.message_user(request, "; ".join(exc.messages), level=messages.ERROR)
            return
        self.message_user(
            request,
            f"{len(approved)} Titel bestätigt und als vorhanden markiert.",
            level=messages.SUCCESS,
        )
