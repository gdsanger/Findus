from .models import Document, DocumentComment


def open_action_status_count(request):
    """Badge count for the nav's "Zu erledigen" shortcut (#1057) -- only

    computed for an authenticated user since `Document.visible_to` scopes
    by the request user.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {
        "open_action_status_count": Document.objects.visible_to(user)
        .filter(action_status=Document.ActionStatus.OPEN)
        .count()
    }


def due_follow_up_count(request):
    """Badge count for the nav's "Dokumente"-Link (#1129) -- überfällige +

    heute fällige Wiedervorlagen offener Dokumente, nicht die gesamte
    Wiedervorlagen-Liste: sonst wäre die Zahl immer hoch und sagte nichts
    über das aus, was tatsächlich dringend ist. Ein `document_id__in`
    gegen die bereits gescopte `visible_to`/`exclude(erledigt)`-Queryset
    bleibt eine einzelne Aggregatabfrage (Subquery), kein Query pro
    Dokument -- diese Funktion läuft auf jeder Seite.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    open_documents = Document.objects.open_visible_to(user)
    return {
        "due_follow_up_count": DocumentComment.objects.due()
        .filter(document__in=open_documents)
        .count()
    }
