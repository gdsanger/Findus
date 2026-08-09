from django import forms

from .letter_bindings import MANUAL_SOURCE, source_choices
from .models import (
    Correspondent,
    LetterDraft,
    LetterTemplate,
    LetterTemplatePlaceholder,
    Tag,
    Task,
    TaskTemplate,
    Vorgang,
)

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


class LetterTemplateForm(forms.ModelForm):
    """Vorlagen-Kopf, Anleitung und die überschreibbaren Layout-Teile (#1094).

    Die Layout-Felder (`layout_*`) sind bewusst *keine* Model-Felder,
    sondern schreiben in das `layout`-JSON: das Layout wächst mit dem
    Renderer (#4b), und für jede neue Option eine eigene Spalte plus
    Migration anzulegen wäre teuer, ohne etwas zu gewinnen. Beim Speichern
    wird das bestehende JSON *aktualisiert*, nicht ersetzt -- so überlebt
    ein Schlüssel, den dieses Formular (noch) nicht kennt, jede
    Bearbeitung.

    `owner`/`departments`/`visibility` bleiben wie bei `TaskTemplateForm`
    Sache der View, Platzhalter werden inline auf der Detailseite gepflegt.
    """

    LAYOUT_FIELDS = ("letterhead", "date_place", "closing")

    layout_letterhead = forms.CharField(
        label="Briefkopf",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2}),
        help_text="Kopfzeile über dem Absenderblock; leer = nur der Absender.",
    )
    layout_date_place = forms.CharField(
        label="Ort in der Datumszeile",
        required=False,
        widget=_TEXT_WIDGET,
    )
    layout_closing = forms.CharField(
        label="Grußformel",
        required=False,
        widget=_TEXT_WIDGET,
    )

    class Meta:
        model = LetterTemplate
        fields = ["name", "description", "category", "instructions", "signature", "logo"]
        labels = {
            "name": "Name",
            "description": "Beschreibung (wann verwende ich das?)",
            "category": "Kategorie",
            "instructions": "Anleitung (Markdown)",
            "signature": "Signatur",
            "logo": "Logo (optional)",
        }
        widgets = {
            "name": _TEXT_WIDGET,
            "description": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 2}
            ),
            "category": forms.TextInput(
                attrs={
                    "class": "form-control form-control-sm",
                    "list": "letter-template-categories",
                    "autocomplete": "off",
                }
            ),
            "instructions": forms.Textarea(
                attrs={
                    "class": "form-control form-control-sm findus-letter-instructions",
                    "rows": 14,
                    "placeholder": "## Ton\n…\n\n## Aufbau\n1. …",
                }
            ),
            "signature": forms.Textarea(
                attrs={"class": "form-control form-control-sm", "rows": 4}
            ),
            "logo": forms.ClearableFileInput(attrs={"class": "form-control form-control-sm"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key in self.LAYOUT_FIELDS:
            self.fields[f"layout_{key}"].initial = (
                self.instance.layout_value(key) if self.instance else None
            )

    def save(self, commit=True):
        template = super().save(commit=False)
        layout = dict(template.layout or {})
        for key in self.LAYOUT_FIELDS:
            layout[key] = self.cleaned_data.get(f"layout_{key}", "").strip()
        template.layout = layout
        if commit:
            template.save()
            self.save_m2m()
        return template


class LetterTemplateDraftForm(forms.Form):
    """Die Eingabe für „Mit KI erstellen" (#1097): kurze Absicht, optional
    Kategorie- und Ton-Hinweis.

    Ein reines `forms.Form` -- es gibt hier nichts zu speichern, die Absicht
    ist Prompt-Material für einen Call und danach vergessen. Das Ergebnis
    füllt `LetterTemplateForm` vor, gespeichert wird erst auf Knopfdruck.
    """

    intent = forms.CharField(
        label="Was soll die Vorlage können?",
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control form-control-sm",
                "rows": 3,
                "placeholder": (
                    "Widerspruch gegen eine Inkasso-Forderung, sachlich-bestimmt, "
                    "mit Fristsetzung"
                ),
            }
        ),
    )
    category_hint = forms.CharField(
        label="Kategorie (optional)",
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "list": "letter-template-categories",
                "autocomplete": "off",
            }
        ),
    )
    tone = forms.CharField(
        label="Ton / Stil (optional)",
        required=False,
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": "form-control form-control-sm", "placeholder": "höflich, aber bestimmt"}
        ),
    )


class LetterDraftStartForm(forms.Form):
    """Der Einstieg in einen KI-Brief (#1095): Vorlage wählen, optional
    Hinweise dazuschreiben.

    Ein `forms.Form` und kein `ModelForm` auf `LetterDraft`: der Entwurf
    wird aus mehr zusammengesetzt, als hier eingegeben wird (Kontext-
    Dokument, aufgelöste Bindungen, Layout-Snapshot), und die
    manuellen Platzhalter sind je nach Vorlage andere Felder -- die hängt
    `add_manual_fields()` dynamisch an.
    """

    MANUAL_PREFIX = "manual_"

    template = forms.ModelChoiceField(
        label="Brief-Vorlage",
        queryset=LetterTemplate.objects.none(),
        empty_label="– Vorlage wählen –",
        widget=_SELECT_WIDGET,
    )
    notes = forms.CharField(
        label="Hinweise für die KI (optional)",
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control form-control-sm",
                "rows": 3,
                "placeholder": (
                    "Worauf es in diesem Schreiben ankommt – z. B. „Frist bis "
                    "31.08. setzen“ oder „kurz und persönlich, ich duze den "
                    "Empfänger“."
                ),
            }
        ),
    )

    def __init__(self, *args, templates=None, template=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = (
            templates if templates is not None else LetterTemplate.objects.none()
        )
        self.manual_placeholders = []
        if template is not None:
            self.add_manual_fields(template)

    def add_manual_fields(self, template):
        """Ein Eingabefeld je `manual`-Platzhalter der Vorlage.

        Nur `manual`: alles andere zieht die Bindungs-Schicht (#1094) selbst
        aus Findus-Daten, und ein zweites, abweichend befülltes Eingabefeld
        daneben wäre eine Einladung zum Widerspruch.
        """
        for placeholder in template.placeholders.all():
            if placeholder.source != MANUAL_SOURCE:
                continue
            self.manual_placeholders.append(placeholder)
            self.fields[f"{self.MANUAL_PREFIX}{placeholder.key}"] = forms.CharField(
                label=placeholder.display_label,
                required=placeholder.required,
                max_length=500,
                widget=_TEXT_WIDGET,
            )

    def manual_values(self):
        return {
            name.removeprefix(self.MANUAL_PREFIX): (value or "").strip()
            for name, value in self.cleaned_data.items()
            if name.startswith(self.MANUAL_PREFIX)
        }

    def manual_fields(self):
        """Die dynamischen Felder für das Template -- `{{ form }}` würde
        sonst auch Vorlage und Hinweise ein zweites Mal ausgeben.
        """
        return [
            self[f"{self.MANUAL_PREFIX}{placeholder.key}"]
            for placeholder in self.manual_placeholders
        ]


class LetterDraftEditForm(forms.ModelForm):
    """Der Review-Editor (#1095): Betreff und Brieftext, sonst nichts.

    Alles andere am Entwurf (Anschrift, Layout, Bindungen) ist Snapshot --
    editierbar ist genau das, was die KI formuliert hat, denn genau daran
    hat der Nutzer das letzte Wort.
    """

    class Meta:
        model = LetterDraft
        fields = ["subject", "body_text"]
        labels = {"subject": "Betreff", "body_text": "Brieftext"}
        widgets = {
            "subject": _TEXT_WIDGET,
            "body_text": forms.Textarea(
                attrs={"class": "form-control form-control-sm findus-letter-body", "rows": 18}
            ),
        }


class LetterTemplatePlaceholderForm(forms.ModelForm):
    """Eine Daten-Bindung (#1094). `source` bekommt seine Auswahl aus der
    Quellen-Registry (`letter_bindings.source_choices`), nicht aus einer
    Model-`choices`-Liste -- eine später registrierte Quelle taucht damit
    ohne Migration und ohne Formular-Änderung im Select auf.
    """

    class Meta:
        model = LetterTemplatePlaceholder
        fields = ["key", "label", "source", "required"]
        labels = {
            "key": "Schlüssel",
            "label": "Bezeichnung",
            "source": "Quelle",
            "required": "Pflicht",
        }
        widgets = {
            "key": forms.TextInput(
                attrs={"class": "form-control form-control-sm", "placeholder": "empfaenger_adresse"}
            ),
            "label": _TEXT_WIDGET,
            "required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, template=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.template = template or getattr(self.instance, "template", None)
        self.fields["source"] = forms.ChoiceField(
            label="Quelle",
            choices=source_choices(),
            widget=_SELECT_WIDGET,
        )

    def clean_key(self):
        """Die UniqueConstraint (template, key) prüft das Formular nicht von
        selbst -- `template` ist kein Formularfeld, also lässt Djangos
        `validate_unique` sie aus und ein doppelter Schlüssel liefe in einen
        IntegrityError statt in eine Fehlermeldung.
        """
        key = self.cleaned_data["key"]
        if self.template is None:
            return key
        duplicates = self.template.placeholders.filter(key=key)
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError(f"Der Schlüssel „{key}“ ist schon vergeben.")
        return key
