from django.db import models


class IntegrationConfig(models.Model):
    INTEGRATION_CHOICES = [
        ("local_git", "Local Git"),
        ("github", "GitHub"),
        ("linear", "Linear"),
        ("slack", "Slack"),
    ]

    integration_type = models.CharField(max_length=20, choices=INTEGRATION_CHOICES, unique=True)
    is_enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.get_integration_type_display()} ({status})"


class Activity(models.Model):
    SOURCE_CHOICES = [
        ("local_git", "Local Git"),
        ("github", "GitHub"),
        ("linear", "Linear"),
        ("slack", "Slack"),
    ]

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    activity_type = models.CharField(max_length=50)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="")
    external_id = models.CharField(max_length=255)

    # Grouping context
    repository = models.CharField(max_length=255, blank=True, default="")
    branch = models.CharField(max_length=255, blank=True, default="")
    ticket_id = models.CharField(max_length=100, blank=True, default="")
    channel_name = models.CharField(max_length=255, blank=True, default="")

    # Status tracking
    status = models.CharField(max_length=100, blank=True, default="")
    previous_status = models.CharField(max_length=100, blank=True, default="")

    # Escape hatch
    metadata = models.JSONField(default=dict, blank=True)

    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id", "activity_type"],
                name="unique_source_external_activity",
            )
        ]
        ordering = ["-occurred_at"]
        verbose_name_plural = "activities"

    def __str__(self):
        return f"[{self.source}] {self.activity_type}: {self.title[:80]}"
