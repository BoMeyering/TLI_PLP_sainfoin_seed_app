from django.contrib import admin
from .models import Job, JobImage


class JobImageInline(admin.TabularInline):
    model = JobImage
    extra = 0
    readonly_fields = ("original_filename", "drive_file_id", "status", "created_at")
    fields = ("original_filename", "drive_file_id", "image", "status", "result", "created_at")


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "status", "source", "conf", "image_count", "created_at")
    list_filter = ("status", "source", "created_at")
    search_fields = ("name", "user__email", "user__username")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [JobImageInline]
    ordering = ("-created_at",)

    def image_count(self, obj):
        return obj.image_count
    image_count.short_description = "Images"


@admin.register(JobImage)
class JobImageAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "job", "status", "seed_count", "pod_count", "created_at")
    list_filter = ("status",)
    search_fields = ("original_filename", "job__name")
    readonly_fields = ("created_at",)

    def seed_count(self, obj):
        return obj.counts.get("seed", "")
    seed_count.short_description = "Seeds"

    def pod_count(self, obj):
        return obj.counts.get("pod", "")
    pod_count.short_description = "Pods"
