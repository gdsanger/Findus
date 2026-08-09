import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

# {# ... #} is Django's single-line comment syntax. A comment that spans a
# newline is NOT stripped by the template engine -- the raw text renders
# verbatim on the page. This regression happened twice already (#1101);
# multi-line explanations belong in {% comment %}...{% endcomment %} instead.
MULTILINE_HASH_COMMENT_RE = re.compile(r"\{#(.*?)#\}", re.DOTALL)

EXCLUDED_DIR_NAMES = {
    ".venv",
    "venv",
    "node_modules",
    "staticfiles",
    "mediafiles",
    ".git",
    "__pycache__",
}


def _iter_template_files(base_dir):
    for path in base_dir.rglob("*.html"):
        if EXCLUDED_DIR_NAMES.intersection(path.parts):
            continue
        if "templates" not in path.parts:
            continue
        yield path


class TemplateCommentLintTests(SimpleTestCase):
    def test_no_multiline_hash_comments_in_templates(self):
        base_dir = Path(settings.BASE_DIR)
        offenders = []
        for template_path in _iter_template_files(base_dir):
            text = template_path.read_text(encoding="utf-8")
            for match in MULTILINE_HASH_COMMENT_RE.finditer(text):
                if "\n" in match.group(1):
                    line_no = text[: match.start()].count("\n") + 1
                    offenders.append(f"{template_path.relative_to(base_dir)}:{line_no}")
        self.assertFalse(
            offenders,
            "Multiline {# ... #} comments found -- use {% comment %}...{% endcomment %} "
            "instead, {# ... #} only works on a single line: " + ", ".join(offenders),
        )
