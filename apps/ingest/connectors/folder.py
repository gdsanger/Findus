"""Ordner-Connector: pollt konfigurierte Eingangsordner und übergibt neue
Dateien an den gemeinsamen Ingest-Kontrakt (`apps.ingest.service`).

Polling statt inotify/watchdog: läuft identisch über Bind-Mounts,
Netzwerkfreigaben (NFS/SMB) und lokale Volumes, ohne plattformspezifische
Events und ohne zusätzliche Abhängigkeit -- siehe PR-Beschreibung.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings

from apps.accounts.models import Department
from apps.documents.models import Document
from apps.ingest.service import ingest_eml_file, ingest_file, sniff_mime_type

logger = logging.getLogger(__name__)

PROCESSED_DIRNAME = "processed"
FAILED_DIRNAME = "failed"


@dataclass(frozen=True)
class WatchFolder:
    path: Path
    department: Optional[str] = None
    visibility: str = Document.Visibility.DEPARTMENT
    on_duplicate: str = "skip"

    @property
    def processed_dir(self) -> Path:
        return self.path / PROCESSED_DIRNAME

    @property
    def failed_dir(self) -> Path:
        return self.path / FAILED_DIRNAME


def get_watch_folders() -> list[WatchFolder]:
    return [
        WatchFolder(
            path=Path(entry["path"]),
            department=entry.get("department"),
            visibility=entry.get("visibility") or Document.Visibility.DEPARTMENT,
            on_duplicate=entry.get("on_duplicate") or "skip",
        )
        for entry in getattr(settings, "FINDUS_INGEST_WATCH_FOLDERS", [])
    ]


def _allowed_extensions() -> set[str]:
    return {
        ext.lower().lstrip(".")
        for ext in getattr(settings, "FINDUS_INGEST_ALLOWED_EXTENSIONS", [])
    }


def _is_candidate(file_path: Path, allowed_extensions: set[str]) -> bool:
    if not file_path.is_file() or file_path.name.startswith("."):
        return False
    if not allowed_extensions:
        return True
    return file_path.suffix.lower().lstrip(".") in allowed_extensions


def _resolve_department(name: Optional[str]) -> Optional[Department]:
    if not name:
        return None
    department, _ = Department.objects.get_or_create(name=name)
    return department


def _move_with_unique_name(file_path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / file_path.name
    if target.exists():
        target = target_dir / f"{target.stem}-{int(time.time())}{target.suffix}"
    shutil.move(str(file_path), str(target))
    return target


def scan_folder(folder: WatchFolder) -> None:
    """One polling pass over a single configured folder.

    Every file is handled independently: a failure on one file is logged
    and the file moved to `failed/`, the scan continues with the rest
    (Fehlertoleranz aus den Anforderungen) instead of aborting the pass.
    """

    if not folder.path.is_dir():
        logger.warning("Ingest-Ordner nicht gefunden, überspringe: %s", folder.path)
        return

    allowed_extensions = _allowed_extensions()
    department = _resolve_department(folder.department)

    for file_path in sorted(folder.path.iterdir()):
        if not _is_candidate(file_path, allowed_extensions):
            continue

        try:
            with file_path.open("rb") as fileobj:
                mime_type = sniff_mime_type(fileobj, filename=file_path.name)
                if mime_type == "message/rfc822":
                    # `.eml` (#1133): Mail als Leitdokument, Anhaenge als
                    # Unterdokumente -- derselbe Ingest-Kontrakt, nur ein
                    # anderer Einstiegspunkt als eine gewoehnliche Datei.
                    result = ingest_eml_file(
                        fileobj,
                        filename=file_path.name,
                        source=Document.Source.FOLDER,
                        department=department,
                        visibility=folder.visibility,
                        origin_metadata={"ingest_folder": str(folder.path)},
                        on_duplicate=folder.on_duplicate,
                    )
                else:
                    result = ingest_file(
                        fileobj,
                        filename=file_path.name,
                        source=Document.Source.FOLDER,
                        department=department,
                        visibility=folder.visibility,
                        origin_metadata={"ingest_folder": str(folder.path)},
                        on_duplicate=folder.on_duplicate,
                    )
            _move_with_unique_name(file_path, folder.processed_dir)
            logger.info(
                "Ingest-Ordner: %s -> Document %s (created=%s, duplicate=%s)",
                file_path.name,
                result.document.id,
                result.created,
                result.duplicate,
            )
        except Exception:
            logger.exception("Ingest-Ordner: Import fehlgeschlagen für %s", file_path)
            _move_with_unique_name(file_path, folder.failed_dir)


def scan_all_folders() -> None:
    for folder in get_watch_folders():
        scan_folder(folder)


def watch_forever(poll_interval_seconds: Optional[float] = None) -> None:
    interval = poll_interval_seconds or getattr(
        settings, "FINDUS_INGEST_POLL_INTERVAL_SECONDS", 10
    )
    logger.info("Ingest-Ordnerüberwachung gestartet (Intervall %ss)", interval)
    while True:
        scan_all_folders()
        time.sleep(interval)
