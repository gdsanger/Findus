from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django_q.tasks import async_task

from .tasks import example_ping_task

# Static placeholder rows for the HTMX search-partial pattern. Replace with
# a real queryset once the Document model lands (see models.py).
EXAMPLE_ROWS = [
    {"title": "Rechnung Muster GmbH", "sender": "Muster GmbH", "date": "2026-06-02"},
    {"title": "Angebot Bürobedarf", "sender": "OfficePlus", "date": "2026-06-14"},
    {"title": "Vertrag Wartung Server", "sender": "IT Systemhaus Nord", "date": "2026-07-01"},
    {"title": "Mahnung Telekom", "sender": "Telekom AG", "date": "2026-07-09"},
]


@login_required
def home(request):
    return render(request, "documents/home.html", {"rows": EXAMPLE_ROWS, "query": ""})


@login_required
def example_search(request):
    query = request.GET.get("q", "").strip().lower()
    if query:
        rows = [
            row
            for row in EXAMPLE_ROWS
            if query in row["title"].lower() or query in row["sender"].lower()
        ]
    else:
        rows = EXAMPLE_ROWS
    return render(
        request,
        "documents/partials/_example_list.html",
        {"rows": rows, "query": query},
    )


@login_required
def example_task_run(request):
    task_id = async_task(example_ping_task, "pong from HTMX demo")
    return render(
        request,
        "documents/partials/_example_task_result.html",
        {"task_id": task_id},
    )
