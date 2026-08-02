"""Kombinierter Poll-Loop für beide Mail-Backends (IMAP + Graph).

Ein Kommando statt zwei: ein einzelner konfigurierter Container soll
wahlweise nur IMAP, nur Graph oder beide gleichzeitig bedienen können,
ohne zwei separate Prozesse/Befehle betreiben zu müssen. Welches Backend
tatsächlich läuft, entscheidet allein `FINDUS_INGEST_MAIL_SOURCES`
(`enabled`-Flag je Backend) -- siehe `mail_imap.get_imap_mailboxes` /
`mail_graph.get_graph_mailboxes`, die bei `enabled=False` je eine leere
Liste liefern.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from django.conf import settings

from apps.ingest.connectors.mail_graph import scan_all_graph_mailboxes
from apps.ingest.connectors.mail_imap import scan_all_imap_mailboxes

logger = logging.getLogger(__name__)


def scan_all_mail_sources() -> None:
    scan_all_imap_mailboxes()
    scan_all_graph_mailboxes()


def watch_mail_forever(poll_interval_seconds: Optional[float] = None) -> None:
    interval = poll_interval_seconds or getattr(
        settings, "FINDUS_INGEST_MAIL_POLL_INTERVAL_SECONDS", 60
    )
    logger.info("Ingest-Mailüberwachung gestartet (Intervall %ss)", interval)
    while True:
        scan_all_mail_sources()
        time.sleep(interval)
