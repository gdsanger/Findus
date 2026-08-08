"""Grampf-Filter fuer Mail-Anhaenge (#1081): sortiert typische Signatur-/
Deko-Bilder (Social-Logos, winzige Inline-Grafiken, Tracking-Pixel) aus,
bevor sie als Unterdokumente angelegt werden. Zentral hier statt in jedem
Connector, damit IMAP- und Graph-Pfad dieselbe Heuristik teilen -- beide
reichen ihre Anhaenge ohnehin durch `apps.ingest.service.ingest_mail`, das
den Body (fuer die `cid:`-Referenzen) und die Anhaenge zusammen kennt.

Sicherheitsnetz: es werden ausschliesslich *Bilder* (`content_type`
`image/*`) verworfen. PDFs/Office/echte Belege rutschen immer durch, auch
wenn sie als `inline` oder winzig markiert sind -- lieber ein Logo zu viel
als einen Beleg zu wenig. Schwellen und An/Aus kommen aus den Settings
(`FINDUS_INGEST_ATTACHMENT_*`), damit sich der Filter pro Deployment
justieren oder ganz abschalten laesst.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import IO, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# `cid:<id>` aus dem HTML-Body: alles bis zum ersten Trenner (Quote, Space,
# schliessende Klammer/Tag) ist die referenzierte Content-ID.
_CID_REFERENCE_RE = re.compile(r"cid:([^\"'\s>)]+)", re.IGNORECASE)


@dataclass(frozen=True)
class AttachmentFilterConfig:
    """Schwellen + Schalter des Grampf-Filters, aus den Settings gelesen."""

    enabled: bool = True
    min_image_bytes: int = 20 * 1024
    min_image_dimension: int = 200

    @classmethod
    def from_settings(cls) -> "AttachmentFilterConfig":
        return cls(
            enabled=bool(
                getattr(settings, "FINDUS_INGEST_ATTACHMENT_FILTER_ENABLED", True)
            ),
            min_image_bytes=int(
                getattr(settings, "FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_BYTES", 20 * 1024)
            ),
            min_image_dimension=int(
                getattr(settings, "FINDUS_INGEST_ATTACHMENT_MIN_IMAGE_DIMENSION", 200)
            ),
        )


def _normalize_cid(content_id: str) -> str:
    """`<abc@host>` -> `abc@host`: Winkelklammern raus, damit sich Header-
    Content-ID und `cid:`-Referenz im Body vergleichen lassen."""
    return (content_id or "").strip().strip("<>").strip()


def referenced_cids(body: Optional[str]) -> set[str]:
    """Alle im (HTML-)Body ueber `cid:` referenzierten Content-IDs."""
    if not body:
        return set()
    return {match.strip() for match in _CID_REFERENCE_RE.findall(body) if match.strip()}


def _is_image(content_type: str) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower().startswith("image/")


def _fileobj_size(fileobj: IO[bytes]) -> int:
    """Groesse in Bytes, ohne den Lesezeiger fuer den spaeteren Ingest zu
    verlieren (immer wieder auf 0 zuruecksetzen)."""
    try:
        fileobj.seek(0, 2)
        size = fileobj.tell()
    except (OSError, ValueError):
        return -1
    finally:
        try:
            fileobj.seek(0)
        except (OSError, ValueError):
            pass
    return size


def _image_dimensions(fileobj: IO[bytes]) -> Optional[tuple[int, int]]:
    """`(width, height)` in Pixeln oder `None`, wenn sich das Bild nicht
    lesen laesst (kein Bild, defekt) -- dann greift der Groessen-Check."""
    try:
        from PIL import Image

        fileobj.seek(0)
        with Image.open(fileobj) as img:
            return img.size
    except Exception:
        return None
    finally:
        try:
            fileobj.seek(0)
        except (OSError, ValueError):
            pass


def _skip_reason(
    *,
    content_type: str,
    content_id: str,
    inline: bool,
    fileobj: IO[bytes],
    referenced: set[str],
    config: AttachmentFilterConfig,
) -> Optional[str]:
    """Grund, den Anhang zu verwerfen, oder `None` (behalten). Nur Bilder
    koennen ueberhaupt verworfen werden -- das ist das Sicherheitsnetz."""
    if not config.enabled:
        return None
    if not _is_image(content_type):
        return None

    normalized = _normalize_cid(content_id)
    if inline or (normalized and normalized in referenced):
        return "Inline-/cid-Bild (Signatur/Deko)"

    size = _fileobj_size(fileobj)
    if config.min_image_bytes and 0 <= size < config.min_image_bytes:
        return f"kleines Bild ({size} Byte < {config.min_image_bytes})"

    if config.min_image_dimension:
        dimensions = _image_dimensions(fileobj)
        if dimensions and min(dimensions) < config.min_image_dimension:
            width, height = dimensions
            return (
                f"kleine Bildmasse ({width}x{height}px < "
                f"{config.min_image_dimension}px)"
            )

    return None


def filter_mail_attachments(
    attachments: list,
    *,
    body: Optional[str],
    config: Optional[AttachmentFilterConfig] = None,
) -> list:
    """Liefert die zu importierenden Anhaenge; Signatur-/Deko-Bilder fallen
    raus (und werden geloggt). `attachments` sind `MailAttachment`-Objekte
    (siehe `apps.ingest.service`), hier ueber Duck-Typing genutzt, um einen
    Import-Zyklus zu vermeiden."""
    cfg = config or AttachmentFilterConfig.from_settings()
    if not cfg.enabled:
        return attachments

    referenced = referenced_cids(body)
    kept: list = []
    for attachment in attachments:
        reason = _skip_reason(
            content_type=getattr(attachment, "content_type", ""),
            content_id=getattr(attachment, "content_id", ""),
            inline=bool(getattr(attachment, "inline", False)),
            fileobj=attachment.fileobj,
            referenced=referenced,
            config=cfg,
        )
        if reason is None:
            kept.append(attachment)
        else:
            logger.info(
                "Ingest-Mail: Anhang '%s' (%s) nicht importiert -- %s",
                getattr(attachment, "filename", "?"),
                getattr(attachment, "content_type", "") or "?",
                reason,
            )
    return kept
