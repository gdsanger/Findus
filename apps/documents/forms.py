from django import forms

from .models import Correspondent, Tag, Vorgang

_TEXT_WIDGET = forms.TextInput(attrs={"class": "form-control form-control-sm"})


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
