from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("documents/<int:pk>/", views.document_detail, name="detail"),
    path("documents/<int:pk>/meta/edit/", views.document_meta_edit, name="meta_edit"),
    path("documents/<int:pk>/meta/", views.document_meta, name="meta"),
]
