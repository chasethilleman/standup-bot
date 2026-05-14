from django.contrib import admin

from .models import Standup


@admin.register(Standup)
class StandupAdmin(admin.ModelAdmin):
    list_display = ["date", "created_at"]
    readonly_fields = ["created_at"]
