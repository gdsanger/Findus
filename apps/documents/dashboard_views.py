"""Cockpit overview (#1065): document/storage KPIs, Erledigung counters,
open Aufgaben, semantic search entry point and quick-add upload -- one
`visible_to`-scoped landing page instead of forcing "how much is in here,
what's open, what's due" to be pieced together from three separate lists.
"""

import datetime

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import BigIntegerField, Count, Q, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.shortcuts import render
from django.utils import timezone

from .models import Document, Task
from .views import PENDING_STATUSES

OPEN_TASKS_LIMIT = 5
DUE_SOON_DAYS = 7


def _document_stats(user):
    """Single aggregated query: totals, `processing_status`/`action_status`
    breakdowns and the storage sum all come back in one DB round-trip
    (conditional `Count`, `KeyTextTransform` on `metadata->>'size'`) --
    no per-document Python loop, no N+1.
    """
    return Document.objects.visible_to(user).aggregate(
        total=Count("id"),
        ready=Count("id", filter=Q(processing_status=Document.ProcessingStatus.READY)),
        failed=Count("id", filter=Q(processing_status=Document.ProcessingStatus.FAILED)),
        in_progress=Count("id", filter=Q(processing_status__in=PENDING_STATUSES)),
        action_open=Count("id", filter=Q(action_status=Document.ActionStatus.OPEN)),
        action_done=Count("id", filter=Q(action_status=Document.ActionStatus.DONE)),
        action_none=Count("id", filter=Q(action_status=Document.ActionStatus.NONE)),
        total_size=Sum(Cast(KeyTextTransform("size", "metadata"), output_field=BigIntegerField())),
    )


def _task_stats(user, today):
    return Task.objects.visible_to(user).aggregate(
        open=Count("id", filter=Q(status=Task.Status.OPEN)),
        overdue=Count("id", filter=Q(status=Task.Status.OPEN, due_date__lt=today)),
    )


@login_required
def dashboard(request):
    today = timezone.localdate()
    due_soon_until = today + datetime.timedelta(days=DUE_SOON_DAYS)

    open_tasks = list(
        Task.objects.visible_to(request.user)
        .filter(status=Task.Status.OPEN)
        .order_by("due_date", "-created_at")[:OPEN_TASKS_LIMIT]
    )

    context = {
        "doc_stats": _document_stats(request.user),
        "task_stats": _task_stats(request.user, today),
        "open_tasks": open_tasks,
        "today": today,
        "due_soon_until": due_soon_until,
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }
    return render(request, "documents/dashboard.html", context)
