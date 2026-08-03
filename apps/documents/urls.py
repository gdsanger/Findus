from django.urls import path

from . import stammdaten, task_template_views, task_views, views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("documents/upload/", views.document_upload, name="upload"),
    path("documents/<int:pk>/", views.document_detail, name="detail"),
    path("documents/<int:pk>/tasks/create/", views.document_task_create, name="document_task_create"),
    path("documents/<int:pk>/original/", views.document_original_download, name="original_download"),
    path("documents/<int:pk>/original/preview/", views.document_original_preview, name="original_preview"),
    path(
        "documents/<int:pk>/original/preview/panel/",
        views.document_original_preview_panel,
        name="original_preview_panel",
    ),
    path("documents/<int:pk>/delete/", views.document_delete, name="delete"),
    path("documents/<int:pk>/meta/edit/", views.document_meta_edit, name="meta_edit"),
    path("documents/<int:pk>/meta/", views.document_meta, name="meta"),
    path(
        "documents/<int:pk>/meta/quick-create/<str:kind>/",
        views.document_meta_quick_create,
        name="meta_quick_create",
    ),
    path(
        "documents/<int:pk>/suggestions/tags/<int:suggestion_id>/accept/",
        views.document_tag_suggestion_accept,
        name="tag_suggestion_accept",
    ),
    path(
        "documents/<int:pk>/suggestions/tags/<int:suggestion_id>/reject/",
        views.document_tag_suggestion_reject,
        name="tag_suggestion_reject",
    ),
    path(
        "documents/<int:pk>/suggestions/vorgaenge/<int:suggestion_id>/accept/",
        views.document_vorgang_suggestion_accept,
        name="vorgang_suggestion_accept",
    ),
    path(
        "documents/<int:pk>/suggestions/vorgaenge/<int:suggestion_id>/reject/",
        views.document_vorgang_suggestion_reject,
        name="vorgang_suggestion_reject",
    ),
    path("stammdaten/", stammdaten.stammdaten_home, name="stammdaten_home"),
    path("stammdaten/<str:kind>/", stammdaten.stammdaten_list, name="stammdaten_list"),
    path("stammdaten/<str:kind>/<int:pk>/edit/", stammdaten.stammdaten_edit, name="stammdaten_edit"),
    path("stammdaten/<str:kind>/<int:pk>/delete/", stammdaten.stammdaten_delete, name="stammdaten_delete"),
    path("tasks/", task_views.task_list, name="task_list"),
    path("tasks/create/", task_views.task_create, name="task_create"),
    path("tasks/template-prefill/", task_views.task_template_prefill, name="task_template_prefill"),
    path("tasks/<int:pk>/", task_views.task_detail, name="task_detail"),
    path("tasks/<int:pk>/toggle/", task_views.task_toggle_status, name="task_toggle_status"),
    path(
        "tasks/<int:pk>/checklist/add/",
        task_views.checklist_item_add,
        name="checklist_item_add",
    ),
    path(
        "tasks/<int:pk>/checklist/<int:item_id>/toggle/",
        task_views.checklist_item_toggle,
        name="checklist_item_toggle",
    ),
    path(
        "tasks/<int:pk>/checklist/<int:item_id>/update/",
        task_views.checklist_item_update,
        name="checklist_item_update",
    ),
    path(
        "tasks/<int:pk>/checklist/<int:item_id>/move/<str:direction>/",
        task_views.checklist_item_move,
        name="checklist_item_move",
    ),
    path(
        "tasks/<int:pk>/checklist/<int:item_id>/delete/",
        task_views.checklist_item_delete,
        name="checklist_item_delete",
    ),
    path("task-templates/", task_template_views.task_template_list, name="task_template_list"),
    path("task-templates/create/", task_template_views.task_template_create, name="task_template_create"),
    path("task-templates/<int:pk>/", task_template_views.task_template_detail, name="task_template_detail"),
    path(
        "task-templates/<int:pk>/delete/",
        task_template_views.task_template_delete,
        name="task_template_delete",
    ),
    path(
        "task-templates/<int:pk>/items/add/",
        task_template_views.task_template_item_add,
        name="task_template_item_add",
    ),
    path(
        "task-templates/<int:pk>/items/<int:item_id>/update/",
        task_template_views.task_template_item_update,
        name="task_template_item_update",
    ),
    path(
        "task-templates/<int:pk>/items/<int:item_id>/move/<str:direction>/",
        task_template_views.task_template_item_move,
        name="task_template_item_move",
    ),
    path(
        "task-templates/<int:pk>/items/<int:item_id>/delete/",
        task_template_views.task_template_item_delete,
        name="task_template_item_delete",
    ),
]
