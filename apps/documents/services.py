"""Absender-Zuordnung: Mailadresse -> `Correspondent`, angelegt bei Bedarf.

Genutzt von den Mail-Ingest-Connectoren (IMAP + Microsoft Graph), aber
bewusst hier statt in `apps.ingest`, weil `Correspondent` ein
Documents-Modell ist und die Zuordnungslogik unabhängig vom
Ingest-Kontrakt nützlich ist.
"""

from __future__ import annotations

from typing import Optional

from django.db import IntegrityError, transaction

from .models import Correspondent


def find_or_create_correspondent_by_email(
    email: str, display_name: str = ""
) -> Optional[Correspondent]:
    """Match `email` against an existing `Correspondent`, or create one.

    Matching is by `email` (case-insensitive), not `name` -- `name` is
    unique but not a reliable identity key for a sender (two senders can
    share a display name, e.g. "Buchhaltung"). On a `name` collision with
    an unrelated `Correspondent`, disambiguate with the address rather
    than silently attaching this mail's address to someone else's record.
    """

    email = (email or "").strip().lower()
    if not email:
        return None

    existing = Correspondent.objects.filter(email__iexact=email).first()
    if existing is not None:
        return existing

    name = (display_name or "").strip() or email
    try:
        # Own savepoint: on a collision below, only this insert rolls
        # back -- not the whole enclosing transaction (request or test).
        with transaction.atomic():
            return Correspondent.objects.create(name=name, email=email)
    except IntegrityError:
        return Correspondent.objects.create(name=f"{name} <{email}>", email=email)


def find_or_create_correspondent(*, name: str = "", email: str = "") -> Optional[Correspondent]:
    """Resolve a sender description (e.g. from the KI-Analyse, #1020) to a

    `Correspondent`, preferring `email` as the identity key -- same
    reasoning as `find_or_create_correspondent_by_email`. Falls back to
    name-only matching/creation when no address was recognized (e.g. a
    scanned letter with no reply address), since name is at least
    `unique` even though it isn't a reliable identity key on its own.
    """

    if email:
        return find_or_create_correspondent_by_email(email, display_name=name)

    name = (name or "").strip()
    if not name:
        return None

    existing = Correspondent.objects.filter(name__iexact=name).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return Correspondent.objects.create(name=name)
    except IntegrityError:
        return Correspondent.objects.filter(name__iexact=name).first()
