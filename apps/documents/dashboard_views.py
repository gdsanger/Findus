"""Cockpit overview (#1065): document/storage KPIs, Erledigung counters,
naechste faellige Wiedervorlagen (#1130), semantic search entry point and
quick-add upload -- one `visible_to`-scoped landing page instead of forcing
"how much is in here, what's open, what's due" to be pieced together from
three separate lists.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import BigIntegerField, Count, Q, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.shortcuts import render

from .models import Document, DocumentComment
from .views import PENDING_STATUSES

FOLLOW_UP_LIMIT = 6


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


def _next_follow_ups(user, limit=FOLLOW_UP_LIMIT):
    """Die naechsten faelligen Wiedervorlagen (#1130) -- ueberfaellige zuerst,

    was sich allein aus der aufsteigenden Sortierung nach `follow_up_date`
    ergibt (ueberfaellig bedeutet per Definition ein Datum vor heute).
    Dieselbe Auswahl wie die Wiedervorlagen-Ansicht (#1129,
    `views._followup_entries`) ueber `Document.objects.open_visible_to` --
    eine gemeinsame Queryset-Methode statt einer zweiten Ausformulierung von
    "sichtbar und noch offen".
    """
    documents = Document.objects.open_visible_to(user)
    return list(
        DocumentComment.objects.with_follow_up()
        .filter(document__in=documents)
        .select_related("document", "document__correspondent")
        .order_by("follow_up_date", "created_at")[:limit]
    )


@login_required
def dashboard(request):
    context = {
        "doc_stats": _document_stats(request.user),
        "follow_ups": _next_follow_ups(request.user),
        "upload_allowed_extensions": settings.FINDUS_INGEST_ALLOWED_EXTENSIONS,
        "upload_max_size_mb": settings.FINDUS_UPLOAD_MAX_SIZE_MB,
    }
    return render(request, "documents/dashboard.html", context)
