from django.urls import path

from . import stammdaten, views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("documents/upload/", views.document_upload, name="upload"),
    path("documents/<int:pk>/", views.document_detail, name="detail"),
    path("documents/<int:pk>/original/", views.document_original_download, name="original_download"),
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
]
