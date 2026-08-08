"""Zentrale, robuste MIME-Typ-Bestimmung fuer alle Ingest-Wege (#1077).

Upload-/Client-Metadaten sind unzuverlaessig: Scanner und manche Upload-Wege
(#1007 Ordner / #1008 Mail / #1019 Upload) melden fuer ein echtes PDF den
generischen Typ `application/octet-stream`. Die Extraktions-Kaskade (#1009)
lehnte diesen dann als "nicht unterstuetzt" ab -- obwohl der Dateiinhalt ein
sauberes `%PDF` ist.

`resolve_mime_type` bestimmt den Typ deshalb primaer aus dem *Inhalt*
(Magic-Bytes, optional libmagic/`python-magic`) und faellt auf die
*Dateiendung* zurueck; die vom Client gemeldete Angabe wird nur genutzt,
wenn sie spezifisch ist und weder Inhalt noch Endung etwas hergeben. So
profitieren alle Ingest-Wege von derselben Normalisierung, und ein als
`octet-stream` eingeliefertes PDF wird zu `application/pdf` korrigiert statt
abgelehnt.
"""

from __future__ import annotations

import logging
import mimetypes
import os

logger = logging.getLogger(__name__)

GENERIC_MIME_TYPE = "application/octet-stream"

# Als "nichtssagend" behandelte Client-Angaben: hier zaehlt allein Inhalt +
# Endung, die gemeldete Angabe wird verworfen.
_GENERIC_MIME_TYPES = {
    "",
    GENERIC_MIME_TYPE,
    "binary/octet-stream",
    "application/binary",
}

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Explizite Endungs-Zuordnung fuer die von der Extraktions-Kaskade
# unterstuetzten Typen -- unabhaengig von den (je nach OS/`mime.types`
# schwankenden) Systemtabellen, die `mimetypes` sonst heranzieht.
_EXTENSION_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".docx": _DOCX_MIME,
    ".txt": "text/plain",
    ".eml": "message/rfc822",
}


def _is_generic(mime_type: str) -> bool:
    return mime_type in _GENERIC_MIME_TYPES


def _sniff_magic_bytes(data: bytes) -> str:
    """Erkennt die kritischen Typen anhand ihrer Magic-Bytes -- ohne jede
    externe Abhaengigkeit und damit auch dann verfuegbar, wenn libmagic
    fehlt. Deckt genau die Formate ab, die als `octet-stream` einzulaufen
    pflegen (v. a. Scanner-PDFs)."""
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _sniff_libmagic(data: bytes) -> str:
    """Zusatzsignal via libmagic/`python-magic`, falls installiert. Fehlt
    das Modul (oder die Shared-Lib), faellt die Bestimmung stillschweigend
    auf Magic-Bytes + Endung zurueck -- die Kernfaelle deckt bereits
    `_sniff_magic_bytes` ab."""
    try:
        import magic  # type: ignore
    except Exception:  # ImportError, aber auch fehlende libmagic zur Laufzeit
        return ""
    try:
        detected = (magic.from_buffer(data, mime=True) or "").strip().lower()
    except Exception:
        logger.debug("libmagic-Erkennung fehlgeschlagen", exc_info=True)
        return ""
    return "" if _is_generic(detected) else detected


def _sniff_content(data: bytes) -> str:
    return _sniff_magic_bytes(data) or _sniff_libmagic(data)


def mime_from_filename(filename: str) -> str:
    """MIME-Typ aus der Dateiendung. Nutzt `os.path.splitext`, sodass
    Leerzeichen im Namen (`001 1.pdf`) die Endungserkennung nicht stoeren,
    und die explizite Tabelle vor der (schwankenden) Systemtabelle."""
    _, extension = os.path.splitext(filename or "")
    extension = extension.lower()
    if extension in _EXTENSION_MIME:
        return _EXTENSION_MIME[extension]
    guessed = (mimetypes.guess_type(filename or "")[0] or "").lower()
    # Ein generischer Treffer (z. B. `.bin` -> octet-stream) ist kein
    # brauchbarer Fallback -- als "unbekannt" behandeln, damit eine
    # spezifische Client-Angabe noch zum Zug kommen kann.
    return "" if _is_generic(guessed) else guessed


def resolve_mime_type(data: bytes, filename: str = "", declared: str = "") -> str:
    """Bester MIME-Typ fuer `data`/`filename`, Client-Angabe `declared`
    nur nachrangig.

    Reihenfolge: Inhalt (Magic-Bytes, dann libmagic) -> Dateiendung ->
    spezifische Client-Angabe -> `application/octet-stream`. Ein als
    `octet-stream` gemeldetes PDF wird so ueber Inhalt/Endung zu
    `application/pdf` korrigiert; erst wenn Inhalt *und* Endung nichts
    hergeben und auch die Client-Angabe generisch ist, bleibt es
    `octet-stream`.
    """
    content = _sniff_content(data)
    if content:
        return content

    extension = mime_from_filename(filename)
    if extension:
        return extension

    declared_norm = (declared or "").strip().lower()
    if not _is_generic(declared_norm):
        return declared_norm

    return GENERIC_MIME_TYPE
