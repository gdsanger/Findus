from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("documents/upload/", views.document_upload, name="upload"),
    path("documents/<int:pk>/", views.document_detail, name="detail"),
    path("documents/<int:pk>/meta/edit/", views.document_meta_edit, name="meta_edit"),
    path("documents/<int:pk>/meta/", views.document_meta, name="meta"),
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
]
