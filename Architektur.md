# Findus — Architektur- & Entscheidungsnotiz

**Status:** v0.3 (lebendes Dokument) · **Stand:** 2026-07-27 · **Träger:** Perculasoft e.K. (Christian Angermeier)

> Diese Notiz hält die bisher getroffenen Grundsatzentscheidungen fest und listet die offenen Punkte. Sie wird fortgeschrieben.

## Vision & Zweck
Ein System, das eingescannte/eingehende Dokumente automatisch **archiviert, vektorisiert und semantisch durchsuchbar** macht — inklusive Querverweisen/Ähnlichkeit, Zuordnung zu **Absender** und **Projekt**, **n:n-Tags**, **Markdown-Ansicht** für schnellen Zugriff, **RAG** und **MCP-Anbindung**. Kernnutzen: Dokumente *wiederfinden*, ohne mühsam suchen zu müssen — gegen den „zu faul zum Suchen / vergammelt im Postfach"-Schmerz.

**Findus ist eine Gedächtnis-/Retrieval-Schicht, kein Collaboration-/DMS-Tool.** Teilen und Zusammenarbeit bleiben bei den gewohnten Kanälen (Mail, Teams, …).

## Zielgruppe / Go-to-Market
- **Beachhead:** kleine IT-Firmen, Freelancer, technikaffine Selbstständige, einzelne Privatiers. Geringe Datenschutz-/Compliance-Hürden, früh adaptierend, tragen es als Multiplikatoren weiter (Community = Distribution).
- **Später, gewinnfinanziert:** Steuerberater / Rechtsanwälte / Praxen — höhere Zahlungsbereitschaft, aber ISO 27001 / TISAX / lokale Lösungen nötig. Erst angehen, wenn das Produkt trägt.
- Der Markt formt das Produkt an der **Oberfläche**; das **Fundament** (Datenmodell, ACL-im-Retrieval, Tenancy) bleibt bewusst opinionated und regretfrei.

## Geschäftsmodell
- **Open-Core:** freier, self-hostbarer Kern gewinnt die Community; bezahlte **Managed-Schicht** (gehostete EU-Inferenz, Wartung, Premium-Connectoren, „MCP-Endpoint der einfach läuft") monetarisiert die, die keine Ops wollen.
- Wichtigste frühe Entscheidung: **wo genau die Linie frei ↔ bezahlt liegt.**
- Preis am zahlenden Segment ausrichten (eher 20–50 €+) statt „superbillig für alle" — das ist ein Zielkonflikt, kein Optimierungsproblem.

## Architektur-Prinzipien
1. **LLM = austauschbare Kante, nicht Kern.** Kern = das „Gehirn" (Ingest → OCR → Embed → Store → permission-aware Retrieval → MCP). Die Generierungs-Schicht ist steckbar: eigene EU-Inferenz (z. B. Mistral) für Managed-Kunden, „bring dein eigenes Modell" via MCP für Power-User → verlagert Kosten **und** DSGVO-Verantwortung auf den Nutzer.
2. **Embeddings von Generierung trennen.** Embeddings (häufig, billig, lokal, einmalig beim Ingest) tragen ~80 % des Nutzens (Suche/Ähnlichkeit/Querverweise) und brauchen **kein** großes LLM. Generierung (RAG-Antworten) ist selten und on-demand → beherrschbare Token-Kosten.
3. **Gehirn als sauber gekapselter Dienst**, Mandanten-Denke von Tag eins.

## Deployment
- **Eine isolierte Findus-Instanz pro Kunde** (Container mit UI + DB), analog zum bewährten **Zenico**-Muster (senkt Ausführungsrisiko: Tooling/Ops existieren).
- Begründung: technische Isolation, Marketing-/Trust-Story („deine Daten, dein Container, auf Wunsch deine Infrastruktur"), Datenschutz (physische Trennung → keine „beweise kein Cross-Tenant-Leak"-Last).
- Der pro Kunde **begrenzte** Datenbestand ist zugleich der Grund, warum pgvector locker reicht — Deployment- und Datenspeicher-Entscheidung verstärken sich.

## Datenhaltung
**Ein Datenspeicher: PostgreSQL.** Kein Mongo daneben (doppelte Ops-Fläche × jeder Container).
- **pgvector** für Vektoren (HNSW) — reicht bei per-Kunde-begrenztem Bestand deutlich.
- **RLS (Row-Level-Security)** erzwingt Sichtbarkeit auf DB-Ebene → auditierbar, nicht app-seitig drangeschraubt.
- **JSONB** für schemalose Dokument-Metadaten (variiert je Dokumenttyp) — Dokumentmodell *und* Relationen in einer Engine.
- **Relationales** (Absender, Projekte, n:n-Tags, Querverweise) als echte Tabellen mit FKs.
- Vorteil: RLS-Filter + Joins + Vektor-Ranking in **einer** garantierten SQL-Query, eine Transaktionsgrenze.

**Trennung Gehirn ↔ Regal:**
- Postgres = **Gehirn** (Metadaten, Text, Markdown-Cache als Textspalte, Vektoren, ACL).
- **Object Storage** (S3-kompatibel / MinIO im Container bzw. Dateisystem) = **Regal** für die Original-Binärdateien (Scans/PDFs). Blobs gehören **nicht** in Postgres (bläht Backups, quält Vacuum).

## Sichtbarkeitsmodell (bewusst minimal)
- **Genau zwei Ebenen: Abteilung sieht alles · privat = nur Besitzer. Nichts dazwischen.**
- Abteilung/Org-Einheit als **Filter/Scope** (`department_ids`, n:n — überlebt Reorgs), **nicht** als harte Tenant-Partition (Tenants sind disjunkt = passt für *Kunde*, nicht für überlappende *Abteilungen*).
- **Kein Peer-to-Peer-Teilen.** ACL-Komplexität steckt in den *Kanten* des Teilen-Graphen (transitiv, Entzug …); keine Kanten → keine Explosion. **Sicherheit durch Abwesenheit des Features** schlägt Sicherheit durch Konfiguration.
- „privat" = **optional, off-by-default**; `owner_id`-Spalte von Anfang an reservieren, Durchsetzung/UI ggf. später (YAGNI). Skepsis, ob überhaupt nötig.
- **Streng vertrauliche Dokumente gehören nicht ins System** — als *ausgesprochene* Produktgrenze kommunizieren (sonst kippt der erste Kunde HR-/Kündigungsakten rein und gibt die Schuld).
- Ziel/Zweck ist explizit **Wissen teilen** innerhalb der Abteilung, nicht abschotten.

## DSGVO
- EU-Inferenz-Wege statt „Grauzone": **Mistral** (EU, AVV), **Azure OpenAI** EU Data Boundary, **AWS Bedrock** (AVV, EU-Region), spezialisierte EU-Inferenz-Hoster. Mit AVV + EU-Residenz verkaufbar → „deine Daten verlassen Europa nicht" wird vom Problem zum **Verkaufsargument**.

## LLM-Anbindung (drei Egress-Modi)
Alle drei sind derselbe steckbare „Stecker", für drei Nutzertypen:
1. **Managed (Mistral, EU):** Findus orchestriert den LLM-Call selbst. Reibungsloser Default für nicht-technische Nutzer; DSGVO-clean; Token-Kosten in Findus' Marge → Preis vorsichtig kalkulieren.
2. **BYOK-in-Findus:** Nutzer hinterlegt eigenen Anthropic-/OpenAI-Key; Findus baut den Prompt und ruft dessen Anbieter. Nutzer lebt in der Findus-UI, Findus kontrolliert Prompt/Qualität. **Entkoppelt Findus' Preis von den Token-Kosten** (Nutzer zahlt Tokens selbst → Preis wird reine Software/Subscription).
3. **BYO-via-MCP:** Findus stellt nur den MCP-Server; der eigene KI-Client des Nutzers (Claude Desktop / ChatGPT) macht das Reasoning. Maximale Flexibilität, aber Prompt-/Qualitätskontrolle liegt beim Client.

- **Kontroll-Achse:** Modi 1 + 2 = Findus kontrolliert den Prompt (konsistente Antwortqualität); Modus 3 = der Nutzer-Client kontrolliert.
- **Modellqualität (Stand 2026):** Mistral ist für Standard-RAG (grounded Q&A über abgerufene Chunks) gut genug; auf harten Reasoning-/Agenten-/Cross-Dokument-Aufgaben liegt es hinter den Frontier-Modellen (Claude Opus, GPT, Gemini). Preis dafür ~10× günstiger + EU. → Frontier über Modus 2/3 als Premium-Option.
- **BYOK-DSGVO-Vorbehalt:** verschiebt die AVV-Beziehung zum LLM-Anbieter zum Kunden (hilft), aber Findus bleibt im Datenfluss, solange es den Prompt baut/sendet. Sauber wird der Shift **nur**, wenn der Egress im Kunden-Container bleibt (direkt Container → Anbieter, nie über zentrale Perculasoft-Infrastruktur). Eigener AVV Perculasoft ↔ Kunde bleibt nötig.
- **Pflichten:** Kunden-Key sicher behandeln (verschlüsselt, pro Container, niemals loggen); dünne Provider-Abstraktion (Tool-Calling/Limits unterscheiden sich); keine identische Ausgabe über alle Backends versprechen.

## Tech-Stack (fixiert, 2026-07-27)
- **Backend/App:** Python + **Django**, UI server-rendered mit **HTMX** (+ Bootstrap) — analog Agira/Zenico-Haus-Stack. **FastAPI** nur wo nötig.
- **DB:** PostgreSQL + **pgvector** (ein Datenspeicher; Weaviate raus).
- **MCP:** eigener Service, als **SSE-Endpoint** startbar; teilt sich die Django-Models/Services (kein paralleler Datenzugriff).
- **Cache/Queue:** **Redis** (App-/semantischer Cache + Broker für Background-Worker).
- **Auth:** Django-Bordmittel (User-Verwaltung), zunächst **kein SSO**.
- **KI-Provider-Schicht:** OpenAI, Claude, Gemini, **Ollama** (lokal) — deckt Embeddings *und* Generierung; ggf. LiteLLM als Unterbau.
- **Deployment:** ein Container pro Kunde (Django + Postgres/pgvector + Redis + Worker + MCP-Service), Docker/Compose ab Tag eins.

## Provider-Neutralität & Lock-in-Hedges
- **Provider-agnostisch** gegen drei Risiken: Verfügbarkeit/Politik (Exportkontrolle, Suspendierung), Regulierung (EU), Kommerz (Preis/Deprecation). Belege Juni/Juli 2026: Anthropic-Suspendierung nach US-Exportkontroll-Order; OpenAI-Agent-Hack ggü. Hugging Face → EU-Regulierungsdruck.
- **Embedding = das eigentliche Lock-in**, nicht die Generierung: Vektoren von Modell A ≠ Modell B → Modellwechsel erzwingt **Re-Index** des ganzen Archivs. Hedge: **Embeddings lokal/offen** (Ollama), Modell **+ Version pro Vektor** speichern, Re-Index als unterstützten Migrationsweg. pgvector-Spalte ist **fix-dimensional** → Dimension bewusst wählen.
- **Cache nach Sichtbarkeits-Scope keyen** (Abteilung/Owner), sonst leckt der Antwort-Cache durch das ACL; bei Dokument-Änderung invalidieren.
- **State isolieren, stateless Compute teilen:** DB pro Container (State); ein zustandsloser Embedder (persistiert/loggt nichts) darf zentral im Netz stehen, ohne die Isolation zu brechen.

## Build-Plan — Step 1 (Prototyp, Customer Zero = Christian)
*Verfeinerung der Ausgangsliste.*
1. **Basisprojekt + Compose-Stack:** Django, PostgreSQL+pgvector, HTMX, **Redis + Background-Worker** (Django-Q2/RQ/Dramatiq, nicht Celery), **MCP-Service (SSE)** als eigener Entrypoint. Docker/Compose gehört hier rein (per-Container-Deployment).
2. **Auth:** Django-User-Verwaltung, kein SSO.
3. **Model-Klassen:** Document, Correspondent (Absender), Project, Tag (n:n), Department/OrgUnit, Chunk/Embedding, Text-/Markdown-Cache-Feld; Sichtbarkeitsfelder `owner_id` + `department_ids`. **RLS im Solo-Prototyp noch NICHT** — Scope über Django-Manager/Querysets; RLS-Policies später bei echtem Multi-User pro Instanz.
4. **Core-Services:** DB-Zugriff; pgvector-Suche; **Mailversand** (SMTP + Graph API, = Benachrichtigung); **KI-Provider-Schicht** (Embeddings + Generierung, model+version-Tracking).
   - Hinweis: **Mail-*Ingest*** (IMAP/Graph-Postfach lesen) ≠ Mailversand — der eigentliche Wert-Keil, kommt in Step 2.

## Offene Fragen / To decide
- Open-Core-Linie exakt ziehen (was frei, was bezahlt).
- Ingest-Connectoren & OCR-Pipeline. **Erster Keil: Mail-Postfach-Überwachung** (löst „vergammelt im Postfach"); dazu Ordner, ggf. Dropbox/OneDrive/SharePoint — meist übers Dateisystem lösbar.
- Embedding-Modell-Wahl (lokal vs. gehostet; Mehrsprachigkeit).
- Markdown-Generierung aus Originalen (Tooling/Pipeline).
- „privat"-Feature: bauen oder weglassen?
- Preis-/Verpackungsdetails je Segment.
- Zeitpunkt & Umfang ISO 27001 / TISAX für das STB/RA-Segment.
