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
from .models import ChecklistItem, Document, Task, TaskTemplate

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
    `apps.documents.views.filtered_documents`.
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


def _task_form_context(request, form, task=None, pending_checklist_texts=None, selected_template_id=None):
    document_id = request.GET.get("document", "").strip()
    selected_document_ids = (
        set(task.documents.values_list("id", flat=True))
        if task is not None
        else ({int(document_id)} if document_id.isdigit() else set())
    )
    context = {
        "task": task,
        "form": form,
        "all_documents": Document.objects.visible_to(request.user),
        "selected_document_ids": selected_document_ids,
    }
    if task is None:
        # Only a "Neue Aufgabe" form can start "aus Vorlage" (#1038) -- an
        # existing task already has its own real checklist further down the
        # detail page, so it has nothing to prefill.
        context["task_templates"] = TaskTemplate.objects.visible_to(request.user)
        context["pending_checklist_texts"] = pending_checklist_texts or []
        context["selected_template_id"] = selected_template_id
    return context


def _parse_template_id(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _template_form_initial(template):
    """Snapshot the template's defaults into `TaskForm` initial values --

    the form stays a plain unbound `TaskForm`, so every value is freely
    editable before saving (#1038, no live binding to the template).
    """
    if template is None:
        return {}
    initial = {"title": template.default_title or template.name}
    if template.default_kind:
        initial["kind"] = template.default_kind
    if template.default_description:
        initial["description"] = template.default_description
    if template.default_due_offset_days is not None:
        initial["due_date"] = timezone.localdate() + datetime.timedelta(
            days=template.default_due_offset_days
        )
    return initial


def _create_checklist_items_from_texts(task, texts):
    for order, text in enumerate(texts):
        text = text.strip()
        if text:
            ChecklistItem.objects.create(task=task, text=text, order=order)


def _set_task_documents(user, task, posted_document_ids):
    """Intersect posted document IDs with `visible_to(user)` before linking

    them to the task (#1052) -- the form only ever renders checkboxes for
    visible documents, but a raw POST can carry any ID, and without this
    filter a user could link a task to a document they otherwise can't see.
    """
    task.documents.set(Document.objects.visible_to(user).filter(pk__in=posted_document_ids))


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
            _set_task_documents(request.user, task, request.POST.getlist("documents"))
            _create_checklist_items_from_texts(task, request.POST.getlist("checklist_text"))
            return redirect("documents:task_detail", pk=task.pk)
        context = _task_form_context(
            request,
            form,
            pending_checklist_texts=request.POST.getlist("checklist_text"),
            selected_template_id=_parse_template_id(request.POST.get("template")),
        )
    else:
        form = TaskForm()
        context = _task_form_context(request, form)

    return render(request, "documents/tasks/form.html", context)


@login_required
def task_template_prefill(request):
    """HTMX endpoint behind the "aus Vorlage anlegen" select (#1038) --

    re-renders the editable Angaben/Checkliste section of the create form
    with the chosen template's defaults, without touching the still-unsaved
    document selection sitting outside this fragment.
    """
    template_id = _parse_template_id(request.GET.get("template"))
    template = None
    if template_id is not None:
        template = TaskTemplate.objects.visible_to(request.user).filter(pk=template_id).first()

    form = TaskForm(initial=_template_form_initial(template))
    pending_checklist_texts = [item.text for item in template.items.all()] if template else []
    context = {"task": None, "form": form, "pending_checklist_texts": pending_checklist_texts}
    return render(request, "documents/tasks/partials/_form_dynamic_fields.html", context)


@login_required
def task_detail(request, pk):
    task = _visible_task(request.user, pk)

    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            _set_task_documents(request.user, task, request.POST.getlist("documents"))
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
