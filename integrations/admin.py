from django.contrib import admin

from .models import Activity, IntegrationConfig


@admin.register(IntegrationConfig)
class IntegrationConfigAdmin(admin.ModelAdmin):
    list_display = ["integration_type", "is_enabled", "last_synced_at"]
    list_filter = ["is_enabled", "integration_type"]


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ["source", "activity_type", "title", "occurred_at"]
    list_filter = ["source", "activity_type"]
    search_fields = ["title", "description"]
    readonly_fields = ["created_at"]
