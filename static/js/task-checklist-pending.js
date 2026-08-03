document.addEventListener("click", function (event) {
    var addButton = event.target.closest("[data-checklist-pending-add]");
    if (addButton) {
        var section = addButton.closest("[data-checklist-pending]");
        var list = section.querySelector("[data-checklist-pending-list]");
        var template = section.querySelector("#task-checklist-pending-row-template");
        list.appendChild(template.content.cloneNode(true));
        return;
    }

    var removeButton = event.target.closest("[data-checklist-pending-remove]");
    if (removeButton) {
        removeButton.closest("[data-checklist-pending-row]").remove();
    }
});
