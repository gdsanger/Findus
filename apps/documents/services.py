"""Absender-Zuordnung: Mailadresse -> `Correspondent`, angelegt bei Bedarf.

Genutzt von den Mail-Ingest-Connectoren (IMAP + Microsoft Graph), aber
bewusst hier statt in `apps.ingest`, weil `Correspondent` ein
Documents-Modell ist und die Zuordnungslogik unabhängig vom
Ingest-Kontrakt nützlich ist.
"""

from __future__ import annotations

from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ChecklistItem, Correspondent, Task, TaskTemplate


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


def match_correspondent_by_ids(
    *, vat_id: str = "", iban: str = "", tax_number: str = ""
) -> Optional[Correspondent]:
    """Look up an existing `Correspondent` by USt-IdNr/IBAN/Steuernummer,

    in that preference order -- these identify a legal entity far more
    reliably than a name (OCR spelling variants, subsidiaries with a
    similar name, ...). Used ahead of name matching so the KI-Analyse
    (#1030) doesn't create a duplicate of an already-known correspondent,
    most importantly a `is_self` one (dedup requirement: the KI must
    never create a second "Ich"-identity as a foreign sender).
    """

    vat_id = (vat_id or "").strip()
    if vat_id:
        match = Correspondent.objects.filter(vat_id__iexact=vat_id).first()
        if match is not None:
            return match

    iban = (iban or "").strip().replace(" ", "").upper()
    if iban:
        match = Correspondent.objects.filter(iban__iexact=iban).first()
        if match is not None:
            return match

    tax_number = (tax_number or "").strip()
    if tax_number:
        match = Correspondent.objects.filter(tax_number__iexact=tax_number).first()
        if match is not None:
            return match

    return None


def find_correspondent(
    *,
    name: str = "",
    email: str = "",
    vat_id: str = "",
    tax_number: str = "",
    iban: str = "",
) -> Optional[Correspondent]:
    """Read-only counterpart to `find_or_create_correspondent` -- same

    USt-IdNr/IBAN/Steuernummer-first, then email, then name lookup, but
    never creates a `Correspondent`. Used for the KI-Analyse's recipient
    side (#1030): a document's recipient is usually "me", and creating a
    fresh record for every recipient description would pollute the
    `is_self` identities that direction detection relies on -- if no
    existing correspondent matches, the caller treats the recipient as
    unknown rather than inventing one.
    """

    existing = match_correspondent_by_ids(vat_id=vat_id, iban=iban, tax_number=tax_number)
    if existing is not None:
        return existing

    email = (email or "").strip().lower()
    if email:
        return Correspondent.objects.filter(email__iexact=email).first()

    name = (name or "").strip()
    if not name:
        return None
    return Correspondent.objects.filter(name__iexact=name).first()


def find_or_create_correspondent(
    *,
    name: str = "",
    email: str = "",
    vat_id: str = "",
    tax_number: str = "",
    iban: str = "",
) -> Optional[Correspondent]:
    """Resolve a sender/recipient description (e.g. from the KI-Analyse,

    #1020/#1030) to a `Correspondent`. Preference order: USt-IdNr/IBAN/
    Steuernummer (`match_correspondent_by_ids`) first -- these survive
    name spelling differences and are what stops the KI from ever
    duplicating a known `is_self` identity -- then `email`, same
    reasoning as `find_or_create_correspondent_by_email`. Falls back to
    name-only matching/creation when nothing else was recognized (e.g. a
    scanned letter with no reply address), since name is at least
    `unique` even though it isn't a reliable identity key on its own.
    """

    existing = match_correspondent_by_ids(vat_id=vat_id, iban=iban, tax_number=tax_number)
    if existing is not None:
        return existing

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


def create_task_from_template(
    template: TaskTemplate, user, overrides: Optional[dict] = None
) -> Task:
    """Turn a `TaskTemplate` (#1037) into a real `Task`, on demand.

    Copies the template's `TaskTemplateItem`s to `ChecklistItem`s rather
    than referencing the template -- a later edit to the template (e.g. a
    changed checklist for next month) must not reach back and alter tasks
    already created from it. `owner`/visibility come from `user`, the one
    actually creating the task, not from the template -- same
    department-less-user fallback as
    `apps.documents.task_views.task_departments_and_visibility` (a Task
    reveals nothing beyond its documents, so template-created tasks use
    the identical department/private scoping).
    """

    overrides = overrides or {}

    title = overrides.get("title") or template.default_title or template.name
    due_date = None
    if template.default_due_offset_days is not None:
        due_date = timezone.now().date() + timezone.timedelta(
            days=template.default_due_offset_days
        )

    departments = list(user.departments.all())
    visibility = Task.Visibility.DEPARTMENT if departments else Task.Visibility.PRIVATE

    task = Task.objects.create(
        title=title,
        kind=template.default_kind or Task.Kind.OTHER,
        description=template.default_description,
        due_date=due_date,
        owner=user,
        visibility=visibility,
    )
    task.departments.set(departments)

    for item in template.items.all():
        ChecklistItem.objects.create(task=task, text=item.text, order=item.order)

    return task
