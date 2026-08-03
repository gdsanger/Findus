from .models import Document


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
