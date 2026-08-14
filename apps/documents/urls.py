from django.urls import path

from . import (
    correspondent_views,
    dashboard_views,
    graph_views,
    letter_template_views,
    letter_views,
    reference_views,
    tag_views,
    task_template_views,
    task_views,
    views,
    vorgang_views,
)

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("dashboard/", dashboard_views.dashboard, name="dashboard"),
    path("graph/", graph_views.graph_view, name="graph"),
    path("graph/neighbors/", graph_views.graph_neighbors, name="graph_neighbors"),
    path("graph/search/", graph_views.graph_search, name="graph_search"),
    path("correspondents/", correspondent_views.correspondent_list, name="correspondent_list"),
    path("correspondents/create/", correspondent_views.correspondent_create, name="correspondent_create"),
    path("correspondents/<int:pk>/", correspondent_views.correspondent_detail, name="correspondent_detail"),
    path(
        "correspondents/<int:pk>/delete/",
        correspondent_views.correspondent_delete,
        name="correspondent_delete",
    ),
    path(
        "correspondents/<int:pk>/upload/",
        correspondent_views.correspondent_document_upload,
        name="correspondent_upload",
    ),
    path(
        "correspondents/<int:pk>/kennungen/",
        reference_views.correspondent_references,
        name="correspondent_references",
    ),
    path(
        "correspondents/<int:pk>/kennungen/create/",
        reference_views.correspondent_reference_create,
        name="correspondent_reference_create",
    ),
    path(
        "correspondents/<int:pk>/kennungen/<int:reference_id>/delete/",
        reference_views.correspondent_reference_delete,
        name="correspondent_reference_delete",
    ),
    path("vorgaenge/", vorgang_views.vorgang_list, name="vorgang_list"),
    path("vorgaenge/create/", vorgang_views.vorgang_create, name="vorgang_create"),
    path("vorgaenge/<int:pk>/", vorgang_views.vorgang_detail, name="vorgang_detail"),
    path("vorgaenge/<int:pk>/delete/", vorgang_views.vorgang_delete, name="vorgang_delete"),
    path("vorgaenge/<int:pk>/upload/", vorgang_views.vorgang_document_upload, name="vorgang_upload"),
    path(
        "vorgaenge/<int:pk>/kennungen/",
        reference_views.vorgang_references,
        name="vorgang_references",
    ),
    path(
        "vorgaenge/<int:pk>/kennungen/create/",
        reference_views.vorgang_reference_create,
        name="vorgang_reference_create",
    ),
    path(
        "vorgaenge/<int:pk>/kennungen/<int:reference_id>/delete/",
        reference_views.vorgang_reference_delete,
        name="vorgang_reference_delete",
    ),
    path(
        "vorgaenge/<int:pk>/empfehlungen/",
        vorgang_views.vorgang_recommendations,
        name="vorgang_recommendations",
    ),
    path(
        "vorgaenge/<int:pk>/empfehlungen/generieren/",
        vorgang_views.vorgang_recommendations_generate,
        name="vorgang_recommendations_generate",
    ),
    path(
        "vorgaenge/<int:pk>/empfehlungen/<int:recommendation_id>/uebernehmen/",
        vorgang_views.vorgang_recommendation_accept,
        name="vorgang_recommendation_accept",
    ),
    path(
        "vorgaenge/<int:pk>/empfehlungen/<int:recommendation_id>/verwerfen/",
        vorgang_views.vorgang_recommendation_dismiss,
        name="vorgang_recommendation_dismiss",
    ),
    path("tags/", tag_views.tag_list, name="tag_list"),
    path("tags/create/", tag_views.tag_create, name="tag_create"),
    path("tags/<int:pk>/", tag_views.tag_detail, name="tag_detail"),
    path("tags/<int:pk>/delete/", tag_views.tag_delete, name="tag_delete"),
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
    path("documents/<int:pk>/thumbnail/", views.document_thumbnail, name="thumbnail"),
    path("documents/<int:pk>/related/", views.document_related, name="related"),
    path("documents/<int:pk>/links/create/", views.document_link_create, name="link_create"),
    path(
        "documents/<int:pk>/links/<int:link_id>/delete/",
        views.document_link_delete,
        name="link_delete",
    ),
    path("documents/<int:pk>/references/", views.document_references, name="references"),
    path(
        "documents/<int:pk>/references/create/",
        views.document_reference_create,
        name="reference_create",
    ),
    path(
        "documents/<int:pk>/references/<int:reference_id>/update/",
        views.document_reference_update,
        name="reference_update",
    ),
    path(
        "documents/<int:pk>/references/<int:reference_id>/delete/",
        views.document_reference_delete,
        name="reference_delete",
    ),
    path(
        "documents/<int:pk>/references/assign/<str:scope>/<int:target_id>/",
        views.document_reference_assign,
        name="reference_assign",
    ),
    path("documents/<int:pk>/delete/", views.document_delete, name="delete"),
    path(
        "documents/<int:pk>/children/<int:child_id>/delete/",
        views.document_child_delete,
        name="child_delete",
    ),
    path(
        "documents/<int:pk>/children/<int:child_id>/detach/",
        views.document_child_detach,
        name="child_detach",
    ),
    path(
        "documents/<int:pk>/action-status/",
        views.document_action_status,
        name="action_status",
    ),
    path(
        "documents/<int:pk>/analysis/status/",
        views.document_analysis_status,
        name="analysis_status",
    ),
    path(
        "documents/<int:pk>/analysis/rerun/",
        views.document_analysis_rerun,
        name="analysis_rerun",
    ),
    path(
        "documents/<int:pk>/reprocess/",
        views.document_reprocess,
        name="reprocess",
    ),
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
    # Brief-Entwürfe (#1095): der Erzeugungs-/Review-Weg. Bewusst eigene
    # URLs unter /briefe/ statt unter /documents/: bis zur Freigabe ist ein
    # Entwurf kein Dokument.
    path("briefe/neu/", letter_views.letter_draft_start, name="letter_draft_start"),
    path("briefe/<int:pk>/", letter_views.letter_draft_detail, name="letter_draft_detail"),
    path("briefe/<int:pk>/panel/", letter_views.letter_draft_panel, name="letter_draft_panel"),
    path("briefe/<int:pk>/aktualisieren/", letter_views.letter_draft_update, name="letter_draft_update"),
    path(
        "briefe/<int:pk>/neu-generieren/",
        letter_views.letter_draft_regenerate,
        name="letter_draft_regenerate",
    ),
    path(
        "briefe/<int:pk>/download/<str:fmt>/",
        letter_views.letter_draft_download,
        name="letter_draft_download",
    ),
    path("briefe/<int:pk>/freigeben/", letter_views.letter_draft_finalize, name="letter_draft_finalize"),
    path("briefe/<int:pk>/verwerfen/", letter_views.letter_draft_delete, name="letter_draft_delete"),
    path("brief-vorlagen/", letter_template_views.letter_template_list, name="letter_template_list"),
    path(
        "brief-vorlagen/create/",
        letter_template_views.letter_template_create,
        name="letter_template_create",
    ),
    # Ohne <pk>: derselbe Endpunkt bedient Neu- und Bearbeiten-Seite, die
    # Vorlage (falls es schon eine gibt) kommt als `template_pk` im POST
    # -- ein Entwurf gehört zum Formular, nicht zu einem Datensatz (#1097).
    path(
        "brief-vorlagen/ki-entwurf/",
        letter_template_views.letter_template_draft,
        name="letter_template_draft",
    ),
    path(
        "brief-vorlagen/<int:pk>/",
        letter_template_views.letter_template_detail,
        name="letter_template_detail",
    ),
    path(
        "brief-vorlagen/<int:pk>/delete/",
        letter_template_views.letter_template_delete,
        name="letter_template_delete",
    ),
    path(
        "brief-vorlagen/<int:pk>/platzhalter/add/",
        letter_template_views.letter_template_placeholder_add,
        name="letter_template_placeholder_add",
    ),
    path(
        "brief-vorlagen/<int:pk>/platzhalter/<int:placeholder_id>/update/",
        letter_template_views.letter_template_placeholder_update,
        name="letter_template_placeholder_update",
    ),
    path(
        "brief-vorlagen/<int:pk>/platzhalter/<int:placeholder_id>/move/<str:direction>/",
        letter_template_views.letter_template_placeholder_move,
        name="letter_template_placeholder_move",
    ),
    path(
        "brief-vorlagen/<int:pk>/platzhalter/<int:placeholder_id>/delete/",
        letter_template_views.letter_template_placeholder_delete,
        name="letter_template_placeholder_delete",
    ),
]
