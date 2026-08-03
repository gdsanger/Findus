"""UI for Aufgaben-Vorlagen (#1038): CRUD plus inline checklist-item

management, sibling of `task_views` and feeding its "aus Vorlage anlegen"
select in `task_views.task_create`.
"""

from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import TaskTemplateForm
from .models import TaskTemplate, TaskTemplateItem
from .task_views import task_departments_and_visibility


def _visible_template(user, pk):
    return get_object_or_404(TaskTemplate.objects.visible_to(user), pk=pk)


@login_required
def task_template_list(request):
    templates = TaskTemplate.objects.visible_to(request.user).order_by("name")
    return render(request, "documents/task_templates/list.html", {"templates": templates})


@login_required
def task_template_create(request):
    if request.method == "POST":
        form = TaskTemplateForm(request.POST)
        if form.is_valid():
            template = form.save(commit=False)
            template.owner = request.user
            departments, visibility = task_departments_and_visibility(request.user)
            template.visibility = visibility
            template.save()
            template.departments.set(departments)
            return redirect("documents:task_template_detail", pk=template.pk)
    else:
        form = TaskTemplateForm()

    return render(request, "documents/task_templates/form.html", {"form": form})


@login_required
def task_template_detail(request, pk):
    template = _visible_template(request.user, pk)

    if request.method == "POST":
        form = TaskTemplateForm(request.POST, instance=template)
        if form.is_valid():
            form.save()
            return redirect("documents:task_template_detail", pk=template.pk)
    else:
        form = TaskTemplateForm(instance=template)

    context = {"template": template, "form": form}
    context.update(_items_context(template))
    return render(request, "documents/task_templates/detail.html", context)


@login_required
@require_POST
def task_template_delete(request, pk):
    template = _visible_template(request.user, pk)
    template.delete()
    return redirect("documents:task_template_list")


def _items_context(template):
    return {"template": template, "items": list(template.items.all())}


def _render_items(request, template):
    return render(request, "documents/task_templates/partials/_items.html", _items_context(template))


@login_required
@require_POST
def task_template_item_add(request, pk):
    template = _visible_template(request.user, pk)
    text = request.POST.get("text", "").strip()
    if text:
        next_order = (template.items.aggregate(Max("order"))["order__max"] or 0) + 1
        TaskTemplateItem.objects.create(template=template, text=text, order=next_order)
    return _render_items(request, template)


@login_required
@require_POST
def task_template_item_update(request, pk, item_id):
    template = _visible_template(request.user, pk)
    item = get_object_or_404(template.items, pk=item_id)
    text = request.POST.get("text", "").strip()
    if text:
        item.text = text
        item.save(update_fields=["text"])
    return _render_items(request, template)


@login_required
@require_POST
def task_template_item_delete(request, pk, item_id):
    template = _visible_template(request.user, pk)
    item = get_object_or_404(template.items, pk=item_id)
    item.delete()
    return _render_items(request, template)


@login_required
@require_POST
def task_template_item_move(request, pk, item_id, direction):
    """Swaps `order` with the previous/next item -- same no-drag-and-drop

    approach as `task_views.checklist_item_move`.
    """
    if direction not in ("up", "down"):
        raise Http404

    template = _visible_template(request.user, pk)
    item = get_object_or_404(template.items, pk=item_id)
    items = list(template.items.all())
    index = items.index(item)

    neighbor_index = index - 1 if direction == "up" else index + 1
    if 0 <= neighbor_index < len(items):
        neighbor = items[neighbor_index]
        item.order, neighbor.order = neighbor.order, item.order
        TaskTemplateItem.objects.bulk_update([item, neighbor], ["order"])

    return _render_items(request, template)
