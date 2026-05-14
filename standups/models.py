from django.db import models

from integrations.models import Activity


class Standup(models.Model):
    date = models.DateField()
    yesterday = models.TextField(blank=True, default="")
    today = models.TextField(blank=True, default="")
    blockers = models.TextField(blank=True, default="")
    raw_ai_response = models.TextField(blank=True, default="")
    activities = models.ManyToManyField(Activity, blank=True)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"Standup for {self.date}"
