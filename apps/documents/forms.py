from django import forms

from .letter_bindings import source_choices
from .models import (
    Correspondent,
    DocumentComment,
    LetterDraft,
    LetterTemplate,
    LetterTemplatePlaceholder,
    Tag,
    Task,
    TaskTemplate,
    Vorgang,
)
from .pdf_editing import PdfEditPlan

_TEXT_WIDGET = forms.TextInput(attrs={"class": "form-control form-control-sm"})
_SELECT_WIDGET = forms.Select(attrs={"class": "form-select form-select-sm"})


class DocumentDateInlineForm(forms.Form):
    """Parst das Dokumentdatum für die Inline-Korrektur in den Übersichten

    (#1140) -- ISO (`document_meta`/AI-Antworten) und deutsche Schreibweise
    (Feldeingabe), beide gleichrangig, statt der lokalisierten Default-Formate
    (die für `de-de` nur `TT.MM.JJJJ` kennen). Ein leeres Feld gilt bewusst als
    ungültig: Löschen des Dokumentdatums ist hier kein Anwendungsfall, das
    bleibt dem Detail (`document_meta`) vorbehalten.
    """

    document_date = forms.DateField(input_formats=["%Y-%m-%d", "%d.%m.%Y"])


class CorrespondentForm(forms.ModelForm):
    class Meta:
        model = Correspondent
        fields = [
            "name",
            "email",
            "phone",
            "address",
            "is_self",
            "is_own_business",
            "vat_id",
            "tax_number",
            "iban",
        ]
        labels = {
            "email": "E-Mail",
            "phone": "Telefon",
            "address": "Adresse",
            "is_self": "Das bin ich",
            "is_own_business": "Das ist meine Firma",
            "vat_id": "USt-IdNr.",
            "tax_number": "Steuernummer",
            "iban": "IBAN",
        }
        help_texts = {
            # "Meine Firma" ist eine Verschärfung von "Das bin ich" (#1112):
            # sie markiert die eigene Identität als Gewerbe/Firma und ist die
            # Signalquelle für die geschäftliche Sphäre der Dokumente.
            "is_own_business": "Nur relevant für eigene Identitäten („Das bin ich“).",
        }
        widgets = {
            "name": _TEXT_WIDGET,
            "email": forms.EmailInput(attrs={"class": "form-control form-control-sm"}),
            "phone": _TEXT_WIDGET,
            "address": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 3}),
            "is_self": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_own_business": forms.CheckboxInput(attrs={"class": "form-check-input"}),
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


class DocumentCommentForm(forms.ModelForm):
    """Covers the Kommentar-Chronik-Eintrag (#1125) -- `document`/`author`

    are set by the view (same split as `TaskForm`/`task.documents`), and
    `remind` is validated against `follow_up_date` here so an inline error
    shows up next to the checkbox instead of a checkbox that silently does
    nothing without a date to hang off.
    """

    class Meta:
        model = DocumentComment
        fields = ["body", "follow_up_date", "remind"]
        labels = {
            "body": "Kommentar",
            "follow_up_date": "Wiedervorlage",
            "remind": "Erinnern",
        }
        widgets = {
            "body": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2}),
            "follow_up_date": forms.DateInput(
                attrs={"class": "form-control form-control-sm", "type": "date"}
            ),
            "remind": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("remind") and not cleaned_data.get("follow_up_date"):
            self.add_error("remind", "Erinnern setzt ein Wiedervorlage-Datum voraus.")
        return cleaned_data


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
    """Der Einstieg in einen KI-Brief (#1095): Vorlage wählen, Empfänger und
    Vorgang bestätigen, Lücken füllen, optional Hinweise dazuschreiben.

    Ein `forms.Form` und kein `ModelForm` auf `LetterDraft`: der Entwurf
    wird aus mehr zusammengesetzt, als hier eingegeben wird (Kontext-
    Dokument, aufgelöste Bindungen, Layout-Snapshot), und welche Felder
    überhaupt abzufragen sind, weiß erst die Auflösung -- die hängt
    `add_value_fields()` dynamisch an.

    Abgefragt wird seit #1138 **jeder Platzhalter ohne Wert**, nicht mehr
    nur die mit Quelle `manual`: ein Entwurf soll nicht daran scheitern,
    dass am Kontakt eine Adresse fehlt. Die Felder ergeben sich aus den
    Bindungen *ohne* die eigenen Eingaben des Nutzers -- würde die
    Auflösung sie mitrechnen, verschwände das Feld beim nächsten
    Vorlagenwechsel unter dem gerade Getippten.
    """

    MANUAL_PREFIX = "manual_"
    SAVE_TO_CONTACT_PREFIX = "am_kontakt_speichern_"

    template = forms.ModelChoiceField(
        label="Brief-Vorlage",
        queryset=LetterTemplate.objects.none(),
        empty_label="– Vorlage wählen –",
        widget=_SELECT_WIDGET,
    )
    recipient = forms.ModelChoiceField(
        label="Empfänger",
        queryset=Correspondent.objects.none(),
        required=False,
        empty_label="– kein Empfänger –",
        widget=_SELECT_WIDGET,
        help_text=(
            "Vorgeschlagen ist der Kontakt des Bezugsdokuments – "
            "überschreibbar, falls die Antwort an jemand anderen geht."
        ),
    )
    vorgang = forms.ModelChoiceField(
        label="Vorgang",
        queryset=Vorgang.objects.none(),
        required=False,
        empty_label="– kein Vorgang –",
        widget=_SELECT_WIDGET,
        help_text="Der Vorgang, in dem das Schreiben abgelegt wird.",
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

    def __init__(
        self,
        *args,
        templates=None,
        template=None,
        bindings=None,
        recipients=None,
        vorgaenge=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = (
            templates if templates is not None else LetterTemplate.objects.none()
        )
        self.fields["recipient"].queryset = (
            recipients if recipients is not None else Correspondent.objects.none()
        )
        self.fields["vorgang"].queryset = (
            vorgaenge if vorgaenge is not None else Vorgang.objects.none()
        )
        self.value_bindings = []
        if template is not None:
            self.add_value_fields(bindings or [])

    def add_value_fields(self, bindings):
        """Ein Eingabefeld je Platzhalter, der (noch) keinen Wert hat.

        Das sind zwei Gruppen, die im Formular gleich aussehen und es auch
        sein sollen: die `manual`-Platzhalter, die die Vorlage bewusst
        abfragt, und die Bindungen, deren Quelle nichts hergab. Für die
        zweite Gruppe nennt die Anzeige den Grund (`missing_reason`), damit
        klar ist, ob der Wert hier hingehört oder besser am Kontakt.

        Ein aufgelöster Wert bekommt *kein* Feld: ein zweites, abweichend
        befülltes Eingabefeld neben der Bindung wäre eine Einladung zum
        Widerspruch.
        """
        for binding in bindings:
            if not binding.is_manual and not binding.is_missing:
                continue
            self.value_bindings.append(binding)
            self.fields[f"{self.MANUAL_PREFIX}{binding.key}"] = forms.CharField(
                label=binding.label,
                # Auch ein Pflicht-Platzhalter bleibt hier optional, wenn er
                # aus einer Bindung stammt: an einem fehlenden Stammdatum
                # soll der Entwurf nicht scheitern (#1138) -- die KI markiert
                # die Lücke dann im Text, statt sie zu erfinden.
                required=binding.required and binding.is_manual,
                max_length=500,
                widget=_TEXT_WIDGET,
            )
            if binding.contact_field:
                self.fields[
                    f"{self.SAVE_TO_CONTACT_PREFIX}{binding.key}"
                ] = forms.BooleanField(
                    label="Am Kontakt speichern",
                    required=False,
                    widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
                )

    def manual_values(self):
        return {
            name.removeprefix(self.MANUAL_PREFIX): (value or "").strip()
            for name, value in self.cleaned_data.items()
            if name.startswith(self.MANUAL_PREFIX)
        }

    def contact_writeback(self):
        """``{Correspondent-Feldname: Wert}`` für die angekreuzten „am
        Kontakt speichern"-Lücken -- was der Aufrufer am Empfänger
        nachtragen soll, damit die Lücke beim nächsten Schreiben zu ist.
        """
        values = {}
        for binding in self.value_bindings:
            if not binding.contact_field:
                continue
            if not self.cleaned_data.get(f"{self.SAVE_TO_CONTACT_PREFIX}{binding.key}"):
                continue
            value = (self.cleaned_data.get(f"{self.MANUAL_PREFIX}{binding.key}") or "").strip()
            if value:
                values[binding.contact_field] = value
        return values

    def value_rows(self):
        """Die dynamischen Felder samt ihrer Bindung fürs Template --
        `{{ form }}` würde sonst auch Vorlage und Hinweise ein zweites Mal
        ausgeben, und die Begründung fehlte daneben.
        """
        rows = []
        for binding in self.value_bindings:
            save_name = f"{self.SAVE_TO_CONTACT_PREFIX}{binding.key}"
            rows.append(
                {
                    "binding": binding,
                    "field": self[f"{self.MANUAL_PREFIX}{binding.key}"],
                    "save_field": self[save_name] if save_name in self.fields else None,
                }
            )
        return rows


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


class PdfPageEditForm(forms.Form):
    """Die Eingabe der Seitenansicht (#1155): welche Seite gedreht, welche
    entfernt wird und wo ein neues Dokument beginnt.

    Die Felder entstehen erst mit der Seitenzahl der konkreten Datei
    (`page_count`), weil sie genau daran hängen: `rotate_<n>` je Seite,
    `split_before_<n>` je Zwischenraum ab Seite 2 und die Mehrfachauswahl
    `delete_pages`. Damit prüft Django selbst, dass keine Seite 0 oder 99
    ankommt -- ein unsinniger Wert landet als Inline-Fehler, nicht als 500
    (CLAUDE.md, "Hub-Seiten und Formulare": nie roh aus `request.POST`).

    Die Reihenfolge der Anwendung (drehen -> löschen -> aufteilen) ist
    nicht Sache des Formulars, sondern von `apps.documents.pdf_editing` --
    hier gilt sie nur insofern, als alle Seitenangaben sich durchgängig auf
    die **Originalnummerierung** beziehen.
    """

    ROTATION_CHOICES = (
        ("0", "0°"),
        ("90", "90°"),
        ("180", "180°"),
        ("270", "270°"),
    )

    def __init__(self, *args, page_count, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_count = page_count
        pages = range(1, page_count + 1)
        self.fields["delete_pages"] = forms.MultipleChoiceField(
            required=False,
            choices=[(str(page), str(page)) for page in pages],
            widget=forms.CheckboxSelectMultiple,
            label="Zu entfernende Seiten",
        )
        for page in pages:
            self.fields[f"rotate_{page}"] = forms.ChoiceField(
                required=False,
                choices=self.ROTATION_CHOICES,
                initial="0",
                label=f"Drehung Seite {page}",
            )
        # Eine Schnittmarke sitzt *zwischen* zwei Seiten; "vor Seite 1"
        # wäre keine Teilung, deshalb beginnt die Reihe bei 2. Bei einem
        # einseitigen Dokument entsteht so gar kein Feld -- Aufteilen ist
        # dort nicht anwendbar.
        for page in range(2, page_count + 1):
            self.fields[f"split_before_{page}"] = forms.BooleanField(
                required=False, label=f"Neues Dokument ab Seite {page}"
            )

    def clean(self):
        cleaned = super().clean()
        deletions = tuple(sorted(int(page) for page in cleaned.get("delete_pages") or ()))
        rotations = {
            page: int(cleaned.get(f"rotate_{page}") or 0)
            for page in range(1, self.page_count + 1)
            if int(cleaned.get(f"rotate_{page}") or 0)
        }
        splits = tuple(
            page
            for page in range(2, self.page_count + 1)
            if cleaned.get(f"split_before_{page}")
        )

        if len(deletions) == self.page_count:
            raise forms.ValidationError(
                "Es sind alle Seiten zum Entfernen markiert. Ein Dokument ohne "
                "Seiten ist kein Dokument – wer alles loswerden will, löscht "
                "das Dokument."
            )
        if not (deletions or rotations or splits):
            raise forms.ValidationError(
                "Es ist nichts ausgewählt, was geändert werden soll."
            )

        cleaned["plan"] = PdfEditPlan(
            rotations=rotations, deletions=deletions, splits=splits
        )
        return cleaned

    @property
    def plan(self):
        """Der validierte Plan -- erst nach `is_valid()` gefüllt."""
        return self.cleaned_data["plan"]
