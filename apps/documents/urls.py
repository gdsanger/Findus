from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.document_list, name="home"),
    path("documents/<int:pk>/", views.document_detail, name="detail"),
]
