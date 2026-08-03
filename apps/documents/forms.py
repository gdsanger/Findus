from django import forms

from .models import Correspondent, Tag, Task, Vorgang

_TEXT_WIDGET = forms.TextInput(attrs={"class": "form-control form-control-sm"})
_SELECT_WIDGET = forms.Select(attrs={"class": "form-select form-select-sm"})


class CorrespondentForm(forms.ModelForm):
    class Meta:
        model = Correspondent
        fields = ["name", "email"]
        widgets = {
            "name": _TEXT_WIDGET,
            "email": forms.EmailInput(attrs={"class": "form-control form-control-sm"}),
        }


class VorgangForm(forms.ModelForm):
    class Meta:
        model = Vorgang
        fields = ["name"]
        widgets = {"name": _TEXT_WIDGET}


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "dimension"]
        widgets = {"name": _TEXT_WIDGET, "dimension": _TEXT_WIDGET}


class TaskForm(forms.ModelForm):
    """Covers title/kind/status/Frist/description -- `documents` (n:n) and

    the `visibility`/`departments`/`owner` scoping are handled separately by
    the views, same split as `Document` (no visibility field in any form
    there either).
    """

    class Meta:
        model = Task
        fields = ["title", "kind", "status", "due_date", "description"]
        widgets = {
            "title": _TEXT_WIDGET,
            "kind": _SELECT_WIDGET,
            "status": _SELECT_WIDGET,
            "due_date": forms.DateInput(
                attrs={"class": "form-control form-control-sm", "type": "date"}
            ),
            "description": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 3}
            ),
        }
