// Token-Eingabe für Vorgänge/Tags im Zuordnungs-Formular (#1136): Tastatur-
// bedienung der Trefferliste plus Chip-Verwaltung. Rein delegiert (kein
// einmaliges Binden beim Laden), damit es nach jedem HTMX-Swap des
// Zuordnungs-Blocks unverändert weiterfunktioniert.

function findusTokenResultItems(field) {
    var results = field.querySelector("[data-token-results]");
    return results ? Array.prototype.slice.call(results.querySelectorAll("[data-token-result]")) : [];
}

function findusTokenSetActive(items, index) {
    items.forEach(function (item) {
        item.classList.remove("is-active");
    });
    if (items[index]) {
        items[index].classList.add("is-active");
        items[index].scrollIntoView({ block: "nearest" });
    }
}

function findusTokenMoveActive(items, direction) {
    if (!items.length) {
        return;
    }
    var current = items.findIndex(function (item) {
        return item.classList.contains("is-active");
    });
    var next = current + direction;
    if (next < 0) {
        next = items.length - 1;
    } else if (next >= items.length) {
        next = 0;
    }
    findusTokenSetActive(items, next);
}

function findusTokenBuildChip(field, id, label) {
    var template = field.closest(".findus-detail-meta").querySelector("[data-token-chip-template]");
    var chip = template.content.firstElementChild.cloneNode(true);
    chip.dataset.id = id;
    var input = chip.querySelector("[data-token-chip-input]");
    input.name = field.dataset.tokenFieldName;
    input.value = id;
    chip.querySelector("[data-token-chip-label]").textContent = label;
    chip.querySelector("[data-token-remove]").setAttribute("aria-label", "„" + label + "“ entfernen");
    return chip;
}

function findusTokenClearSearch(field) {
    var results = field.querySelector("[data-token-results]");
    var search = field.querySelector("[data-token-search]");
    if (results) {
        results.innerHTML = "";
    }
    if (search) {
        search.value = "";
        search.focus();
    }
}

function findusTokenCreate(field, chips, rawValue) {
    var values = { chip: "1" };
    values[field.dataset.tokenKind + "_name"] = rawValue;
    htmx.ajax("POST", field.dataset.tokenCreateUrl, {
        target: chips,
        swap: "beforeend",
        values: values,
    });
}

function findusTokenSelect(result) {
    var field = result.closest("[data-token-field]");
    var chips = field.querySelector("[data-token-chips]");

    if (result.dataset.tokenCreate === "1") {
        findusTokenCreate(field, chips, result.dataset.tokenValue);
    } else {
        var id = result.dataset.tokenId;
        if (!chips.querySelector('[data-id="' + id + '"]')) {
            chips.appendChild(findusTokenBuildChip(field, id, result.dataset.tokenLabel));
            htmx.trigger(chips, "token-save");
        }
    }

    findusTokenClearSearch(field);
}

document.addEventListener("click", function (event) {
    var removeButton = event.target.closest("[data-token-remove]");
    if (removeButton) {
        var chip = removeButton.closest("[data-token-chip]");
        var chips = chip.closest("[data-token-chips]");
        chip.remove();
        htmx.trigger(chips, "token-save");
        return;
    }

    var result = event.target.closest("[data-token-result]");
    if (result) {
        event.preventDefault();
        findusTokenSelect(result);
    }
});

document.addEventListener("keydown", function (event) {
    var search = event.target.closest("[data-token-search]");
    if (!search) {
        return;
    }
    var field = search.closest("[data-token-field]");
    var items = findusTokenResultItems(field);

    if (event.key === "ArrowDown") {
        event.preventDefault();
        findusTokenMoveActive(items, 1);
    } else if (event.key === "ArrowUp") {
        event.preventDefault();
        findusTokenMoveActive(items, -1);
    } else if (event.key === "Enter") {
        event.preventDefault();
        var active = items.filter(function (item) {
            return item.classList.contains("is-active");
        })[0] || items[0];
        if (active) {
            findusTokenSelect(active);
        }
    } else if (event.key === "Escape") {
        if (items.length) {
            event.preventDefault();
            field.querySelector("[data-token-results]").innerHTML = "";
        }
    } else if (event.key === "Backspace" && search.value === "") {
        var chips = field.querySelector("[data-token-chips]");
        var lastChip = chips.lastElementChild;
        if (lastChip) {
            lastChip.remove();
            htmx.trigger(chips, "token-save");
        }
    }
});

document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (target && target.matches && target.matches("[data-token-results]")) {
        var first = target.querySelector("[data-token-result]");
        if (first) {
            first.classList.add("is-active");
        }
    }
});
