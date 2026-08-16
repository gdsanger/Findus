// Inline-Bearbeitung des Dokumentdatums in den Übersichten (#1140): Klick auf
// die Anzeige blendet ein Eingabefeld ein, Enter/Verlassen speichert per
// eigenem `findus-date-save`-Event statt per nativem `blur`/`keyup[Enter]`
// direkt -- so kann Escape den Blur auslösen, um das Feld zu schließen, ohne
// dass der Save mitgerissen wird (der `cancelled`-Marker unterdrückt ihn).
// Delegiert auf `document`-Ebene, damit jeder HTMX-Swap der Übersicht
// (Pagination, Filterwechsel, ein erfolgreicher Save selbst) weiterhin
// funktioniert -- siehe `document-meta-tokens.js` für dasselbe Prinzip.

function findusDateWrap(el) {
    return el.closest(".findus-doc-date-inline");
}

function findusDateEnterEdit(display) {
    var wrap = findusDateWrap(display);
    var input = wrap.querySelector("[data-doc-date-input]");
    wrap.classList.add("is-editing");
    input.value = input.dataset.originalValue;
    input.focus();
    input.select();
}

document.addEventListener("click", function (event) {
    var display = event.target.closest("[data-doc-date-display]");
    if (display) {
        findusDateEnterEdit(display);
    }
});

document.addEventListener("keydown", function (event) {
    var display = event.target.closest("[data-doc-date-display]");
    if (display && event.key === "Enter") {
        event.preventDefault();
        findusDateEnterEdit(display);
        return;
    }

    var input = event.target.closest("[data-doc-date-input]");
    if (!input) {
        return;
    }
    if (event.key === "Enter") {
        event.preventDefault();
        input.blur();
    } else if (event.key === "Escape") {
        input.dataset.cancelled = "1";
        input.value = input.dataset.originalValue;
        input.blur();
    }
});

// `focusout` statt `blur` direkt am Feld, damit ein einziger Listener sowohl
// den echten Fokusverlust als auch das programmatische `blur()` aus den
// beiden Faellen oben (Enter/Escape) abdeckt.
document.addEventListener(
    "focusout",
    function (event) {
        var input = event.target.closest("[data-doc-date-input]");
        if (!input) {
            return;
        }
        var wrap = findusDateWrap(input);
        if (input.dataset.cancelled === "1") {
            input.dataset.cancelled = "";
            wrap.classList.remove("is-editing");
            return;
        }
        wrap.classList.remove("is-editing");
        htmx.trigger(input, "findus-date-save");
    },
    true
);

// Bleibt eine Eingabe ungueltig, rendert der Endpunkt das Partial erneut im
// Bearbeitungszustand (`is-editing`, siehe `_document_date_inline.html`) --
// Fokus zurueck aufs Feld, sonst muesste man nach der Fehlermeldung erneut
// klicken, um weiterzutippen.
document.body.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail.target;
    if (!target || !target.matches || !target.matches(".findus-doc-date-inline")) {
        return;
    }
    if (target.classList.contains("is-editing")) {
        var input = target.querySelector("[data-doc-date-input]");
        if (input) {
            input.focus();
            input.select();
        }
    }
});
