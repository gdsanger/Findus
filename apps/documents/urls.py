from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("", views.home, name="home"),
    path("example/search/", views.example_search, name="example-search"),
    path("example/task/", views.example_task_run, name="example-task-run"),
]
