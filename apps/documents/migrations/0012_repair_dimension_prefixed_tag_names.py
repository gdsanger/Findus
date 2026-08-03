from django.db import migrations


def _split_dimension_prefix(name, dimension):
    prefix, sep, rest = name.partition(":")
    if not sep or not rest.strip():
        return None
    return rest.strip(), (dimension or prefix.strip())


def repair_dimension_prefixed_names(apps, schema_editor):
    """One-off cleanup for #1034: a KI reply that crammed "Dimension:Wert"

    into `name` while also filling `dimension` left stray `Tag`/
    `TagSuggestion` rows with a colon-prefixed name. Split them apart the
    same way `_normalize_tag_fields` now does for new suggestions, and
    merge into an already-correct Tag if one exists so the unique
    `(name, dimension)` constraint isn't violated.
    """
    Tag = apps.get_model("documents", "Tag")
    TagSuggestion = apps.get_model("documents", "TagSuggestion")

    for tag in Tag.objects.filter(name__contains=":"):
        split = _split_dimension_prefix(tag.name, tag.dimension)
        if split is None:
            continue
        name, dimension = split
        target = Tag.objects.filter(name=name, dimension=dimension).exclude(pk=tag.pk).first()
        if target is not None:
            for document in tag.documents.all():
                target.documents.add(document)
            tag.delete()
        else:
            tag.name = name
            tag.dimension = dimension
            tag.save(update_fields=["name", "dimension"])

    for suggestion in TagSuggestion.objects.filter(name__contains=":"):
        split = _split_dimension_prefix(suggestion.name, suggestion.dimension)
        if split is None:
            continue
        suggestion.name, suggestion.dimension = split
        suggestion.save(update_fields=["name", "dimension"])


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0011_correspondent_iban_correspondent_is_self_and_more"),
    ]

    operations = [
        migrations.RunPython(repair_dimension_prefixed_names, migrations.RunPython.noop),
    ]
