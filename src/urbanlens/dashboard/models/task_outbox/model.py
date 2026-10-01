"""A Celery task the broker refused, kept until it can be queued."""

from __future__ import annotations

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import CharField, DateTimeField, Index, JSONField, PositiveIntegerField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.task_outbox.queryset import TaskOutboxEntryManager


class TaskOutboxEntry(abstract.DashboardModel):
    """One enqueue that failed, replayed by ``tasks.drain_task_outbox``.

    Written by ``services.core.celery.safely_enqueue_task`` only when the broker refuses a message, in the
    caller's transaction, so the work survives exactly when the change that asked for it does.
    """

    task_name = CharField(max_length=200)
    args = JSONField(default=list, encoder=DjangoJSONEncoder)
    kwargs = JSONField(default=dict, encoder=DjangoJSONEncoder)
    #: Empty for the task's own declared queue.
    queue = CharField(max_length=64, blank=True, default="")
    #: When the caller's ``countdown`` would have released it.
    not_before = DateTimeField(null=True, blank=True)
    #: When the caller's ``expires`` would have dropped it.
    expires_at = DateTimeField(null=True, blank=True)
    attempts = PositiveIntegerField(default=0)
    next_attempt_at = DateTimeField()
    last_error = CharField(max_length=255, blank=True, default="")

    objects = TaskOutboxEntryManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_task_outbox"
        ordering = ["next_attempt_at"]
        indexes = [Index(fields=["next_attempt_at"], name="idxdb_taskoutbox_next")]

    def __str__(self) -> str:
        return f"TaskOutboxEntry({self.task_name}, {self.attempts} attempts)"
