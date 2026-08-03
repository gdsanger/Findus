"""UI for the Aufgaben-Ebene (#1012): list/filter, create/edit, checklist,

document linking -- the model has existed since #1012 but had no way to
reach it outside the admin (#6/#1023).
"""

import datetime

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import TaskForm
from .models import ChecklistItem, Document, Task

TASKS_PAGE_SIZE = 20

DUE_FILTER_CHOICES = [
    ("", "Alle"),
    ("overdue", "Überfällig"),
    ("week", "Nächste 7 Tage"),
    ("none", "Ohne Frist"),
]


def task_departments_and_visibility(user):
    """Same department-less-user fallback as

    `apps.documents.views._upload_departments_and_visibility` -- a Task
    reveals nothing beyond its documents, so it uses the identical
    department/private scoping (see `Task.Visibility`).
    """
    departments = list(user.departments.all())
    if departments:
        return departments, Task.Visibility.DEPARTMENT
    return departments, Task.Visibility.PRIVATE


def _visible_task(user, pk):
    """No `prefetch_related("checklist_items")` here -- the checklist

    mutation views (add/toggle/delete) share this helper and each need a
    fresh read straight after writing, which a cached prefetch would mask.
    """
    return get_object_or_404(
        Task.objects.visible_to(user).prefetch_related("documents"),
        pk=pk,
    )


def _filtered_tasks(request):
    """Apply the combinable Status/Frist filters on top of the visibility

    scope, same "filters narrow, never widen" principle as
    `apps.documents.views._filtered_documents`.
    """
    tasks = Task.objects.visible_to(request.user).prefetch_related("documents")

    status = request.GET.get("status", "").strip()
    if status:
        tasks = tasks.filter(status=status)

    due = request.GET.get("due", "").strip()
    today = timezone.localdate()
    if due == "overdue":
        tasks = tasks.filter(due_date__lt=today, status=Task.Status.OPEN)
    elif due == "week":
        tasks = tasks.filter(due_date__gte=today, due_date__lte=today + datetime.timedelta(days=7))
    elif due == "none":
        tasks = tasks.filter(due_date__isnull=True)

    return tasks


@login_required
def task_list(request):
    tasks = _filtered_tasks(request)
    paginator = Paginator(tasks, TASKS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "status_choices": Task.Status.choices,
        "due_choices": DUE_FILTER_CHOICES,
        "selected": {
            "status": request.GET.get("status", ""),
            "due": request.GET.get("due", ""),
        },
    }
    return render(request, "documents/tasks/list.html", context)


def _task_form_context(request, form, task=None):
    document_id = request.GET.get("document", "").strip()
    selected_document_ids = (
        set(task.documents.values_list("id", flat=True))
        if task is not None
        else ({int(document_id)} if document_id.isdigit() else set())
    )
    return {
        "task": task,
        "form": form,
        "all_documents": Document.objects.visible_to(request.user),
        "selected_document_ids": selected_document_ids,
    }


@login_required
def task_create(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.owner = request.user
            departments, visibility = task_departments_and_visibility(request.user)
            task.visibility = visibility
            task.save()
            task.departments.set(departments)
            task.documents.set(request.POST.getlist("documents"))
            return redirect("documents:task_detail", pk=task.pk)
    else:
        form = TaskForm()

    return render(request, "documents/tasks/form.html", _task_form_context(request, form))


@login_required
def task_detail(request, pk):
    task = _visible_task(request.user, pk)

    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            task.documents.set(request.POST.getlist("documents"))
            return redirect("documents:task_detail", pk=task.pk)
    else:
        form = TaskForm(instance=task)

    context = _task_form_context(request, form, task=task)
    context.update(_checklist_context(task))
    return render(request, "documents/tasks/detail.html", context)


@login_required
@require_POST
def task_toggle_status(request, pk):
    """Single-purpose companion to the full edit form -- flips only

    `status` (and, through `Task.save`, `done_at`), so ticking a task off
    doesn't require resubmitting every field through `TaskForm`.
    """
    task = _visible_task(request.user, pk)
    task.status = Task.Status.OPEN if task.status == Task.Status.DONE else Task.Status.DONE
    task.save()
    return redirect("documents:task_detail", pk=task.pk)


def _checklist_context(task):
    checklist_items = list(task.checklist_items.all())
    done_count = sum(1 for item in checklist_items if item.is_done)
    total_count = len(checklist_items)
    return {
        "task": task,
        "checklist_items": checklist_items,
        "done_count": done_count,
        "total_count": total_count,
        "progress_percent": round(done_count / total_count * 100) if total_count else 0,
    }


def _render_checklist(request, task):
    return render(request, "documents/tasks/partials/_checklist.html", _checklist_context(task))


@login_required
@require_POST
def checklist_item_add(request, pk):
    task = _visible_task(request.user, pk)
    text = request.POST.get("text", "").strip()
    if text:
        next_order = (task.checklist_items.aggregate(Max("order"))["order__max"] or 0) + 1
        ChecklistItem.objects.create(task=task, text=text, order=next_order)
    return _render_checklist(request, task)


@login_required
@require_POST
def checklist_item_toggle(request, pk, item_id):
    task = _visible_task(request.user, pk)
    item = get_object_or_404(task.checklist_items, pk=item_id)
    item.is_done = not item.is_done
    item.save(update_fields=["is_done", "updated_at"])
    return _render_checklist(request, task)


@login_required
@require_POST
def checklist_item_update(request, pk, item_id):
    task = _visible_task(request.user, pk)
    item = get_object_or_404(task.checklist_items, pk=item_id)
    text = request.POST.get("text", "").strip()
    if text:
        item.text = text
        item.save(update_fields=["text", "updated_at"])
    return _render_checklist(request, task)


@login_required
@require_POST
def checklist_item_delete(request, pk, item_id):
    task = _visible_task(request.user, pk)
    item = get_object_or_404(task.checklist_items, pk=item_id)
    item.delete()
    return _render_checklist(request, task)


@login_required
@require_POST
def checklist_item_move(request, pk, item_id, direction):
    """Swaps `order` with the previous/next item -- no drag-and-drop, but

    it covers the "sortieren" requirement without pulling in a JS library
    for what is, in practice, short lists of steps within a task.
    """
    if direction not in ("up", "down"):
        raise Http404

    task = _visible_task(request.user, pk)
    item = get_object_or_404(task.checklist_items, pk=item_id)
    items = list(task.checklist_items.all())
    index = items.index(item)

    neighbor_index = index - 1 if direction == "up" else index + 1
    if 0 <= neighbor_index < len(items):
        neighbor = items[neighbor_index]
        item.order, neighbor.order = neighbor.order, item.order
        ChecklistItem.objects.bulk_update([item, neighbor], ["order"])

    return _render_checklist(request, task)
