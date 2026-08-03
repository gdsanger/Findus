from django import forms

from .models import Correspondent, Tag, Task, TaskTemplate, Vorgang

_TEXT_WIDGET = forms.TextInput(attrs={"class": "form-control form-control-sm"})
_SELECT_WIDGET = forms.Select(attrs={"class": "form-select form-select-sm"})


class CorrespondentForm(forms.ModelForm):
    class Meta:
        model = Correspondent
        fields = ["name", "email", "address", "is_self", "vat_id", "tax_number", "iban"]
        labels = {
            "email": "E-Mail",
            "address": "Adresse",
            "is_self": "Das bin ich",
            "vat_id": "USt-IdNr.",
            "tax_number": "Steuernummer",
            "iban": "IBAN",
        }
        widgets = {
            "name": _TEXT_WIDGET,
            "email": forms.EmailInput(attrs={"class": "form-control form-control-sm"}),
            "address": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
            "is_self": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "vat_id": _TEXT_WIDGET,
            "tax_number": _TEXT_WIDGET,
            "iban": _TEXT_WIDGET,
        }


class VorgangForm(forms.ModelForm):
    class Meta:
        model = Vorgang
        fields = ["name", "description", "status", "department"]
        labels = {"description": "Beschreibung", "status": "Status", "department": "Abteilung"}
        widgets = {
            "name": _TEXT_WIDGET,
            "description": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
            "status": _SELECT_WIDGET,
            "department": _SELECT_WIDGET,
        }


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "dimension", "color"]
        labels = {"dimension": "Dimension", "color": "Farbe"}
        widgets = {
            "name": _TEXT_WIDGET,
            "dimension": _TEXT_WIDGET,
            "color": forms.TextInput(attrs={"class": "form-control form-control-sm", "type": "color"}),
        }


class TaskForm(forms.ModelForm):
    """Covers title/kind/status/Frist/description -- `documents` (n:n) and

    the `visibility`/`departments`/`owner` scoping are handled separately by
    the views, same split as `Document` (no visibility field in any form
    there either).
    """

    class Meta:
        model = Task
        fields = ["title", "kind", "status", "due_date", "description"]
        labels = {
            "title": "Titel",
            "kind": "Art",
            "status": "Status",
            "due_date": "Frist",
            "description": "Beschreibung",
        }
        widgets = {
            "title": _TEXT_WIDGET,
            "kind": _SELECT_WIDGET,
            "status": _SELECT_WIDGET,
            "due_date": forms.DateInput(
                attrs={"class": "form-control form-control-sm", "type": "date"}
            ),
            "description": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 4}
            ),
        }


class TaskTemplateForm(forms.ModelForm):
    """Covers the blueprint fields of `TaskTemplate` (#1038) -- `owner`/

    `departments`/`visibility` are scoped by the view from the creating
    user, same split as `TaskForm`. The nested `TaskTemplateItem` checklist
    is managed inline on the detail page, not through this form.
    """

    class Meta:
        model = TaskTemplate
        fields = [
            "name",
            "default_kind",
            "default_title",
            "default_description",
            "default_due_offset_days",
        ]
        labels = {
            "name": "Name",
            "default_kind": "Art (Standard)",
            "default_title": "Titel (Standard)",
            "default_description": "Beschreibung (Standard)",
            "default_due_offset_days": "Frist (Tage nach Anlage)",
        }
        widgets = {
            "name": _TEXT_WIDGET,
            "default_kind": _SELECT_WIDGET,
            "default_title": _TEXT_WIDGET,
            "default_description": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 3}
            ),
            "default_due_offset_days": forms.NumberInput(
                attrs={"class": "form-control form-control-sm", "min": 0}
            ),
        }
