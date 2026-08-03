document.addEventListener("input", function (event) {
    if (!event.target.matches("[data-task-document-filter]")) return;

    var query = event.target.value.trim().toLowerCase();
    var list = event.target.closest(".findus-task-documents").querySelector("[data-task-document-list]");
    list.querySelectorAll("[data-document-title]").forEach(function (row) {
        var matches = query === "" || row.dataset.documentTitle.indexOf(query) !== -1;
        row.classList.toggle("d-none", !matches);
    });
});
