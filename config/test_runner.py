"""Testrunner mit projektweiten Voreinstellungen.

Warum ein eigener Runner statt Kommandozeilen-Optionen: Die Tests werden auch
von automatisierten Läufen gestartet, die schlicht `manage.py test` aufrufen.
Alles, was hier steht, gilt unabhängig davon, wie und von wem der Lauf
gestartet wurde — eine Option, die niemand mitgibt, wirkt nicht.

Aktiviert über  TEST_RUNNER = "config.test_runner.FindusTestRunner"
"""

import shutil
import tempfile

from django.test.runner import DiscoverRunner, get_max_test_processes
from django.test.utils import override_settings

# Im Test genügt ein billiger Hasher: Die Suite legt wegen der durchgängigen
# Sichtbarkeitsprüfungen ständig Nutzer an, und das Hashen mit voller
# Rundenzahl ist dabei reine Rechenverschwendung.
# NIEMALS in produktive Settings übernehmen.
TEST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


class FindusTestRunner(DiscoverRunner):
    """Läuft standardmäßig parallel, hasht billig, schreibt in ein Wegwerf-
    Medienverzeichnis.
    """

    def __init__(self, *args, parallel=0, **kwargs):
        # parallel=0 ist Djangos "nicht angegeben" -> Voreinstellung setzen.
        # Ein ausdrückliches `--parallel 1` bleibt seriell, `--parallel 4`
        # bleibt bei 4: die Kommandozeile sticht die Voreinstellung.
        if not parallel or parallel == "auto":
            parallel = get_max_test_processes()
        super().__init__(*args, parallel=parallel, **kwargs)

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)

        # Eigenes Medienverzeichnis je Lauf: hält das Arbeitsverzeichnis sauber
        # und verhindert, dass sich Läufe über liegengebliebene Dateien
        # gegenseitig beeinflussen.
        self._media_root = tempfile.mkdtemp(prefix="findus-test-media-")

        # override_settings statt direkter Zuweisung an `settings`: Nur so wird
        # das setting_changed-Signal ausgelöst, das den zwischengespeicherten
        # Speicherort von default_storage zurücksetzt. Eine schlichte Zuweisung
        # käme beim Storage nicht an.
        self._overrides = override_settings(
            PASSWORD_HASHERS=TEST_PASSWORD_HASHERS,
            MEDIA_ROOT=self._media_root,
        )
        self._overrides.enable()

    def teardown_test_environment(self, **kwargs):
        self._overrides.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().teardown_test_environment(**kwargs)