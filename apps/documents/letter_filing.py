"""Freigabe: aus dem Brief-Entwurf wird ein Dokument am Vorgang (#1095).

Der letzte Schritt des Review-first-Ablaufs -- und der einzige, der etwas
Bleibendes anlegt. Vorher ist alles Entwurf: nichts liegt im Archiv,
nichts hängt am Vorgang, nichts geht raus (Versand ist #5).

Das Ergebnis ist ein **normales Findus-Dokument**, kein Sonderfall: PDF
als `original_file` (browserfähige Vorschau, und das ist die Fassung, die
gedruckt/verschickt wird), Vorgang- und Kontakt-Zuordnung gesetzt,
`direction=ausgang`, `source=generiert_brief`. Der Word-Master bleibt am
Entwurf und ist von der Dokumentseite aus verlinkt -- ihn als zweites
Dokument abzulegen hieße, dieselbe Sache zweimal im Archiv zu haben, mit
zwei Zuordnungen, die auseinanderlaufen können.

Extraktion und KI-Analyse werden übersprungen (`process_document_task`
statt `extract_document_task`): Text, Datum, Richtung, Kontakt und
Dokumentart sind hier *bekannt* -- sie aus dem gerade selbst erzeugten PDF
zurückzulesen und von einem Modell raten zu lassen, wäre bezahlte Arbeit
für ein schlechteres Ergebnis. Das Embedding läuft trotzdem, sonst wäre
der eigene Schriftverkehr das Einzige, was die Suche nicht findet.
"""

from __future__ import annotations

import hashlib
import logging

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .letter_render import build_content, plain_text, render_draft_files
from .models import Document, LetterDraft, link_documents
from .text_sanitize import clean_text

logger = logging.getLogger(__name__)


def _key_facts(draft) -> dict:
    """Die Key-Facts, die sonst die KI-Analyse (#1020) extrahieren würde --
    hier direkt gesetzt, weil sie aus erster Hand bekannt sind.

    Nur belegte Felder: ein leerer Schlüssel im `key_facts`-Dict sieht in
    der Detailansicht aus wie „geprüft und nichts gefunden", nicht wie
    „gar nicht vorhanden".
    """
    facts = {
        "sender_name": draft.sender.name if draft.sender else "",
        "sender_email": draft.sender.email if draft.sender else "",
        "recipient_name": draft.recipient.name if draft.recipient else "",
        "recipient_email": draft.recipient.email if draft.recipient else "",
        "document_date": draft.letter_date.isoformat() if draft.letter_date else "",
        "document_type": "Brief",
    }
    if draft.ai_model:
        facts["ai_model"] = draft.ai_model
        facts["ai_model_version"] = draft.ai_model_version
    return {key: value for key, value in facts.items() if value}


def _document_title(draft, content) -> str:
    if content.subject.strip():
        return content.subject.strip()[:255]
    recipient = draft.recipient.name if draft.recipient else "Empfänger"
    return f"Schreiben an {recipient}"[:255]


@transaction.atomic
def finalize_letter_draft(draft: LetterDraft, user) -> Document:
    """Legt den freigegebenen Entwurf als Dokument ab und gibt es zurück.

    Idempotent: ein zweiter Klick (oder ein zweiter Tab) liefert das
    bereits abgelegte Dokument zurück, statt denselben Brief ein zweites
    Mal ins Archiv zu legen.

    `user` ist der Freigebende -- er wird Besitzer des Dokuments, und nur
    die für *ihn* sichtbaren Verknüpfungen werden gesetzt.
    """
    if draft.document_id is not None:
        return draft.document

    if not draft.pdf_file:
        # Kann passieren, wenn das Rendering nach der Generierung
        # fehlgeschlagen ist (siehe `letter_generation._render_quietly`) --
        # dann jetzt nachholen, statt die Freigabe zu verweigern.
        render_draft_files(draft)

    content = build_content(draft)
    with draft.pdf_file.open("rb") as handle:
        pdf_bytes = handle.read()

    text = clean_text(plain_text(content))
    filename = draft.pdf_file.name.rsplit("/", 1)[-1]

    document = Document(
        title=_document_title(draft, content),
        source=Document.Source.GENERATED_LETTER,
        direction=Document.Direction.AUSGANG,
        correspondent=draft.recipient,
        document_date=draft.letter_date,
        owner=user,
        visibility=draft.visibility,
        processing_status=Document.ProcessingStatus.PENDING,
        text_content=text,
        markdown=text,
        extraction_method=Document.ExtractionMethod.TEXT_LAYER,
        key_facts=_key_facts(draft),
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        metadata={
            "original_filename": filename,
            "mime_type": "application/pdf",
            "size": len(pdf_bytes),
            # Provenienz: woraus dieses Dokument entstanden ist -- neben
            # `source=generiert_brief` die Spur zurück zu Entwurf und Vorlage.
            "letter_draft_id": draft.pk,
            "letter_template_id": draft.template_id,
            "letter_template_name": draft.template_name,
            # Extraktions-Provenienz (#1142), wie beim Kaskaden-Pfad in
            # `extraction.py` -- hier bekannt statt gemessen, weil der Text
            # aus dem gerade erzeugten PDF stammt statt aus der Kaskade.
            "extracted_at": timezone.now().isoformat(),
        },
    )
    document.original_file.save(filename, ContentFile(pdf_bytes), save=False)
    document.save()
    document.departments.set(draft.departments.all())

    if draft.vorgang_id is not None:
        document.vorgaenge.add(draft.vorgang)

    if draft.source_document_id is not None:
        # Querverweis „gehört zu" (#1088) statt `parent`: Anschreiben und
        # Antwort sind zwei eigenständige Dokumente mit eigenem Kontakt und
        # eigener Richtung, kein Leitdokument mit Anhang. Die Richtung der
        # Aussage „X ist die Antwort auf Y" steckt nicht im Link (der ist
        # bewusst ungerichtet, siehe `DocumentLink`), sondern in den beiden
        # Dokumenten selbst: das Schreiben trägt `direction=ausgang` und
        # `source=generiert_brief`, das beantwortete Dokument nicht.
        source_document = (
            Document.objects.visible_to(user).filter(pk=draft.source_document_id).first()
        )
        if source_document is not None:
            link_documents(
                document,
                source_document,
                created_by=user,
                note="Antwortschreiben",
            )
            if draft.vorgang_id is None:
                # Kein Vorgang am Entwurf (Altbestand, oder das
                # Bezugsdokument hing an keinem): dann erbt der Brief die
                # Akte des beantworteten Dokuments. Steht ein Vorgang am
                # Entwurf, bleibt es bei genau diesem -- er war die
                # ausdrückliche Wahl beim Erzeugen (#1138), und die weiteren
                # Vorgänge des Bezugsdokuments sind sie nicht.
                document.vorgaenge.add(*source_document.vorgaenge.all())

    draft.document = document
    draft.status = LetterDraft.Status.FINALIZED
    draft.save(update_fields=["document", "status", "updated_at"])

    transaction.on_commit(lambda: _enqueue_embedding(document.pk))
    logger.info(
        "Brief-Entwurf %s freigegeben -> Document %s (Vorgaenge=%s)",
        draft.pk,
        document.pk,
        list(document.vorgaenge.values_list("pk", flat=True)),
    )
    return document


def _enqueue_embedding(document_id: int) -> None:
    """Nur Chunking/Embedding (#1010) -- Extraktion und KI-Analyse entfallen
    (siehe Modul-Docstring). Nach dem Commit eingereiht, damit der Worker
    das Dokument auch wirklich schon vorfindet.
    """
    from django_q.tasks import async_task

    from .tasks import process_document_task

    async_task(process_document_task, document_id)
