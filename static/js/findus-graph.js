/*
 * Fokus-Graph (#1091) -- Cytoscape.js-Frontend zu /graph.
 *
 * Der Graph wird nie am Stück geladen: er startet bei genau einem
 * Fokus-Knoten und wächst nur dort, wo jemand aufklappt. Jede Expansion ist
 * derselbe Aufruf von `graph/neighbors/` mit dem angeklickten Knoten als
 * Fokus -- Erstaufbau und Aufklappen laufen deshalb durch denselben Code.
 *
 * Interaktion bewusst so herum: Einfachklick klappt auf/zu (die Geste, die
 * man im Graphen dauernd braucht), Doppelklick öffnet Detail/Hub (die
 * Geste, die die Seite verlässt). Ein Fehlklick kostet damit nie den
 * aufgebauten Graphen.
 *
 * Farben kommen aus den CSS-Custom-Properties des Themes (siehe
 * findus.css, `--findus-graph-*`) statt aus Literalen hier: Cytoscape
 * rendert auf Canvas und erbt kein CSS, ein Theme-Wechsel soll die Palette
 * aber trotzdem an genau einer Stelle haben.
 */
(function () {
    "use strict";

    var container = document.getElementById("graph-canvas");
    if (!container || typeof cytoscape === "undefined") {
        return;
    }

    var NEIGHBORS_URL = container.dataset.neighborsUrl;
    var DOCUMENT_PREFIX = "document:";
    // Wie lange ein Einfachklick wartet, ob noch ein zweiter folgt --
    // ohne das würde jeder Doppelklick erst eine Expansion auslösen, die
    // niemand wollte und deren Request beim Navigieren verfällt.
    var DOUBLE_CLICK_GRACE_MS = 250;

    var statusLine = document.getElementById("graph-status");
    var emptyHint = document.getElementById("graph-empty");
    var tooltip = document.getElementById("graph-tooltip");
    var inspector = document.getElementById("graph-inspector");
    var inspectorTitle = document.getElementById("graph-inspector-title");
    var inspectorMeta = document.getElementById("graph-inspector-meta");
    var inspectorOpen = document.getElementById("graph-inspector-open");
    var inspectorExpand = document.getElementById("graph-inspector-expand");
    var inspectorFocus = document.getElementById("graph-inspector-focus");
    var similarityToggle = document.querySelector("[data-graph-similarity]");

    var theme = getComputedStyle(document.documentElement);
    function themeColor(name, fallback) {
        return theme.getPropertyValue(name).trim() || fallback;
    }

    var palette = {
        document: themeColor("--findus-graph-document", "#6ea8fe"),
        vorgang: themeColor("--findus-graph-vorgang", "#79c0a6"),
        tag: themeColor("--findus-graph-tag", "#d9a75f"),
        correspondent: themeColor("--findus-graph-correspondent", "#c98fc0"),
        structure: themeColor("--findus-graph-edge-structure", "#6b6f76"),
        reference: themeColor("--findus-graph-edge-reference", "#d98b5f"),
        similarity: themeColor("--findus-graph-edge-similarity", "#8f7fd1"),
        text: themeColor("--findus-text", "#e8e6e2"),
        muted: themeColor("--findus-text-muted", "#a3a19d"),
        surface: themeColor("--findus-bg", "#1e1f22"),
        accent: themeColor("--findus-accent", "#cc785c")
    };

    var state = {
        focusId: container.dataset.focusNode || null,
        // nodeId -> { similarity: bool } -- `similarity` hält fest, ob die
        // Expansion die kNN-Kanten schon mitgeholt hat. Wer den Toggle erst
        // später einschaltet, bekommt sie für diese Knoten nachgeliefert,
        // statt sie stillschweigend zu vermissen.
        expanded: {},
        hiddenTypes: {},
        similarity: similarityToggle ? similarityToggle.checked : true,
        pendingTap: null
    };

    var cy = cytoscape({
        container: container,
        wheelSensitivity: 0.2,
        minZoom: 0.2,
        maxZoom: 3,
        style: [
            {
                selector: "node",
                style: {
                    width: 20,
                    height: 20,
                    label: "data(label)",
                    color: palette.text,
                    "font-size": "11px",
                    "text-valign": "bottom",
                    "text-margin-y": 6,
                    "text-wrap": "ellipsis",
                    "text-max-width": "130px",
                    "text-background-color": palette.surface,
                    "text-background-opacity": 0.75,
                    "text-background-padding": "2px",
                    "text-background-shape": "roundrectangle",
                    "border-width": 2,
                    "border-color": palette.surface,
                    "background-color": palette.document
                }
            },
            { selector: 'node[type="document"]', style: { "background-color": palette.document } },
            { selector: 'node[type="vorgang"]', style: { "background-color": palette.vorgang, shape: "round-rectangle" } },
            { selector: 'node[type="tag"]', style: { "background-color": palette.tag, shape: "diamond" } },
            { selector: 'node[type="correspondent"]', style: { "background-color": palette.correspondent, shape: "ellipse" } },
            {
                selector: "node.is-focus",
                style: { width: 30, height: 30, "border-width": 3, "border-color": palette.accent, "font-weight": "bold" }
            },
            { selector: "node.is-expanded", style: { "border-color": palette.muted } },
            { selector: "node:selected", style: { "border-color": palette.accent, "border-width": 3 } },
            {
                selector: "edge",
                style: {
                    "curve-style": "bezier",
                    width: 1.5,
                    "line-color": palette.structure,
                    opacity: 0.85
                }
            },
            {
                /* Gemeinsame Kennung (#1099): kräftiger und durchgezogen --
                   ein exakter Treffer ist eine härtere Aussage als jede
                   Ähnlichkeit und darf im Graphen nicht wie eine aussehen.
                   Beschriftet mit der Kennung selbst, denn sie ist die
                   Begründung der Kante. */
                selector: 'edge[kind="reference"]',
                style: {
                    "line-color": palette.reference,
                    width: 3,
                    label: "data(label)",
                    "font-size": "9px",
                    color: palette.muted,
                    "text-background-color": palette.surface,
                    "text-background-opacity": 0.7,
                    "text-background-padding": "1px"
                }
            },
            {
                selector: 'edge[kind="similarity"]',
                style: {
                    "line-color": palette.similarity,
                    "line-style": "dashed",
                    label: "data(label)",
                    "font-size": "9px",
                    color: palette.muted,
                    "text-background-color": palette.surface,
                    "text-background-opacity": 0.7,
                    "text-background-padding": "1px"
                }
            },
            { selector: ".is-filtered-out", style: { display: "none" } }
        ],
        layout: { name: "preset" }
    });

    function setStatus(message) {
        if (statusLine) {
            statusLine.textContent = message || "";
        }
    }

    function splitNodeId(nodeId) {
        var separator = nodeId.indexOf(":");
        return { type: nodeId.slice(0, separator), pk: nodeId.slice(separator + 1) };
    }

    function fetchNeighbors(nodeId, withSimilarity) {
        var parts = splitNodeId(nodeId);
        var url =
            NEIGHBORS_URL +
            "?type=" + encodeURIComponent(parts.type) +
            "&id=" + encodeURIComponent(parts.pk) +
            "&similarity=" + (withSimilarity ? "1" : "0");
        return fetch(url, { credentials: "same-origin" }).then(function (response) {
            if (!response.ok) {
                throw new Error("Knoten nicht ladbar (" + response.status + ")");
            }
            return response.json();
        });
    }

    /* Nur wirklich neue Elemente hinzufügen: dieselbe Kante kann aus zwei
       Richtungen kommen (A aufklappen, dann B), und Cytoscape wirft bei
       doppelten IDs. */
    function merge(payload) {
        var fresh = [];
        payload.nodes.forEach(function (node) {
            if (cy.getElementById(node.id).empty()) {
                fresh.push({ group: "nodes", data: node });
            }
        });
        payload.edges.forEach(function (edge) {
            if (cy.getElementById(edge.id).empty()) {
                fresh.push({ group: "edges", data: edge });
            }
        });
        var added = cy.add(fresh);
        applyFilters();
        return added;
    }

    function runLayout(fit) {
        var layout = cy.layout({
            name: "cose",
            animate: false,
            randomize: false,
            fit: !!fit,
            padding: 40,
            idealEdgeLength: 110,
            nodeRepulsion: 9000,
            nodeOverlap: 12
        });
        layout.run();
    }

    function applyFilters() {
        cy.nodes().forEach(function (node) {
            node.toggleClass("is-filtered-out", state.hiddenTypes[node.data("type")] === true);
        });
        cy.edges('[kind="similarity"]').toggleClass("is-filtered-out", !state.similarity);
    }

    function reportTruncation(payload) {
        if (payload.truncated) {
            setStatus(
                "Nur die " + payload.limit + " nächstliegenden Nachbarn werden gezeigt – " +
                "es gibt mehr. Für die vollständige Liste das Detail/den Hub öffnen."
            );
        } else {
            setStatus("");
        }
    }

    function expand(nodeId) {
        var node = cy.getElementById(nodeId);
        if (node.empty()) {
            return Promise.resolve();
        }
        if (state.expanded[nodeId]) {
            collapse(nodeId);
            return Promise.resolve();
        }
        setStatus("Nachbarn werden geladen …");
        return fetchNeighbors(nodeId, state.similarity)
            .then(function (payload) {
                merge(payload);
                state.expanded[nodeId] = { similarity: state.similarity };
                cy.getElementById(nodeId).addClass("is-expanded");
                runLayout(false);
                reportTruncation(payload);
            })
            .catch(function (error) {
                setStatus(error.message);
            });
    }

    /* Einklappen entfernt nur, was ohne diesen Knoten nicht mehr im Graphen
       hinge: der Fokus, selbst aufgeklappte Knoten und alles, was noch eine
       zweite Kante hat, bleibt stehen. Sonst risse ein Zuklappen Teile des
       Graphen mit heraus, die jemand über einen anderen Weg aufgebaut hat. */
    function collapse(nodeId) {
        var node = cy.getElementById(nodeId);
        var removable = node.neighborhood("node").filter(function (neighbour) {
            return (
                neighbour.id() !== state.focusId &&
                !state.expanded[neighbour.id()] &&
                neighbour.connectedEdges().length <= 1
            );
        });
        cy.remove(removable);
        delete state.expanded[nodeId];
        node.removeClass("is-expanded");
        hideInspector();
        setStatus("");
    }

    function showInspector(node) {
        if (!inspector) {
            return;
        }
        inspector.hidden = false;
        inspectorTitle.textContent = node.data("label");
        inspectorMeta.textContent = (node.data("meta") || []).join(" · ");
        inspectorOpen.setAttribute("href", node.data("url"));
        inspectorExpand.dataset.nodeId = node.id();
        inspectorFocus.dataset.nodeId = node.id();
        inspectorExpand.textContent = state.expanded[node.id()]
            ? "Nachbarn einklappen"
            : "Nachbarn aufklappen";
    }

    function hideInspector() {
        if (inspector) {
            inspector.hidden = true;
        }
    }

    function setFocus(nodeId) {
        cy.elements().remove();
        state.expanded = {};
        state.focusId = nodeId;
        hideInspector();
        if (emptyHint) {
            emptyHint.hidden = true;
        }
        syncUrl();
        setStatus("Graph wird geladen …");
        return fetchNeighbors(nodeId, state.similarity)
            .then(function (payload) {
                merge(payload);
                state.expanded[nodeId] = { similarity: state.similarity };
                cy.getElementById(nodeId).addClass("is-focus").addClass("is-expanded");
                runLayout(true);
                reportTruncation(payload);
            })
            .catch(function (error) {
                setStatus(error.message);
            });
    }

    /* Fokus und Ähnlichkeits-Schalter in die Adresszeile spiegeln, damit ein
       Graph teilbar bleibt -- `replaceState`, weil jede Expansion sonst
       einen History-Eintrag erzeugte und "Zurück" durch die eigene
       Klickspur statt zur vorherigen Seite führte. */
    function syncUrl() {
        if (!state.focusId || !window.history || !window.history.replaceState) {
            return;
        }
        var parts = splitNodeId(state.focusId);
        var url = new URL(window.location.href);
        url.searchParams.set("type", parts.type);
        url.searchParams.set("id", parts.pk);
        url.searchParams.set("similarity", state.similarity ? "1" : "0");
        window.history.replaceState({}, "", url.toString());
    }

    // -- Interaktion ---------------------------------------------------------

    cy.on("tap", "node", function (event) {
        var node = event.target;
        showInspector(node);
        if (state.pendingTap) {
            window.clearTimeout(state.pendingTap);
        }
        state.pendingTap = window.setTimeout(function () {
            state.pendingTap = null;
            expand(node.id()).then(function () {
                showInspector(node);
            });
        }, DOUBLE_CLICK_GRACE_MS);
    });

    cy.on("dbltap", "node", function (event) {
        if (state.pendingTap) {
            window.clearTimeout(state.pendingTap);
            state.pendingTap = null;
        }
        window.location.href = event.target.data("url");
    });

    cy.on("tap", function (event) {
        if (event.target === cy) {
            hideInspector();
        }
    });

    cy.on("mouseover", "node", function (event) {
        if (!tooltip) {
            return;
        }
        var node = event.target;
        tooltip.textContent = node.data("label") + " — " + (node.data("meta") || []).join(" · ");
        tooltip.hidden = false;
        var position = node.renderedPosition();
        tooltip.style.left = position.x + "px";
        tooltip.style.top = position.y + "px";
    });

    cy.on("mouseout", "node", function () {
        if (tooltip) {
            tooltip.hidden = true;
        }
    });

    cy.on("pan zoom drag", function () {
        if (tooltip) {
            tooltip.hidden = true;
        }
    });

    document.querySelectorAll("[data-graph-node-filter]").forEach(function (checkbox) {
        checkbox.addEventListener("change", function () {
            state.hiddenTypes[checkbox.dataset.graphNodeFilter] = !checkbox.checked;
            applyFilters();
        });
    });

    if (similarityToggle) {
        similarityToggle.addEventListener("change", function () {
            state.similarity = similarityToggle.checked;
            applyFilters();
            syncUrl();
            if (!state.similarity) {
                return;
            }
            // Nachziehen für Knoten, die ohne Ähnlichkeit aufgeklappt wurden.
            var pending = Object.keys(state.expanded).filter(function (nodeId) {
                return nodeId.indexOf(DOCUMENT_PREFIX) === 0 && !state.expanded[nodeId].similarity;
            });
            if (!pending.length) {
                return;
            }
            setStatus("Ähnlichkeiten werden nachgeladen …");
            Promise.all(
                pending.map(function (nodeId) {
                    return fetchNeighbors(nodeId, true).then(function (payload) {
                        merge(payload);
                        state.expanded[nodeId].similarity = true;
                    });
                })
            )
                .then(function () {
                    runLayout(false);
                    setStatus("");
                })
                .catch(function (error) {
                    setStatus(error.message);
                });
        });
    }

    if (inspectorExpand) {
        inspectorExpand.addEventListener("click", function () {
            var nodeId = inspectorExpand.dataset.nodeId;
            if (nodeId) {
                expand(nodeId).then(function () {
                    var node = cy.getElementById(nodeId);
                    if (!node.empty()) {
                        showInspector(node);
                    }
                });
            }
        });
    }

    if (inspectorFocus) {
        inspectorFocus.addEventListener("click", function () {
            if (inspectorFocus.dataset.nodeId) {
                setFocus(inspectorFocus.dataset.nodeId);
            }
        });
    }

    var fitButton = document.querySelector("[data-graph-fit]");
    if (fitButton) {
        fitButton.addEventListener("click", function () {
            cy.fit(undefined, 40);
        });
    }

    var resetButton = document.querySelector("[data-graph-reset]");
    if (resetButton) {
        resetButton.addEventListener("click", function () {
            if (state.focusId) {
                setFocus(state.focusId);
            }
        });
    }

    /* Treffer der Fokus-Suche sind echte Links (teilbar, funktionieren ohne
       JS) -- hier werden sie nur abgefangen, damit das Setzen eines neuen
       Fokus ohne Reload auskommt. Delegiert, weil HTMX die Liste austauscht. */
    document.addEventListener("click", function (event) {
        var hit = event.target.closest ? event.target.closest("[data-graph-focus]") : null;
        if (!hit || event.metaKey || event.ctrlKey || event.shiftKey) {
            return;
        }
        event.preventDefault();
        setFocus(hit.dataset.graphFocus);
    });

    if (state.focusId) {
        setFocus(state.focusId);
    }
})();
