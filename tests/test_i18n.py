import os
from pathlib import Path
import subprocess
import unittest


class TranslationTests(unittest.TestCase):
    def test_german_catalog_and_english_fallback(self):
        project = Path(__file__).resolve().parent.parent
        code = """
import i18n
import grub_manager
print(i18n._("Search"))
print(i18n.ngettext("{count} configuration found.", "{count} configurations found.", 2).format(count=2))
try:
    grub_manager.extract_entries("", "")
except ValueError as exc:
    print(exc)
"""
        for language, expected in (
            ("de_DE:de", ["Suchen", "2 Konfigurationen gefunden.",
                          "Dateisystem-UUID fehlt oder ist ungültig"]),
            ("zz", ["Search", "2 configurations found.",
                    "Filesystem UUID is missing or invalid"]),
        ):
            with self.subTest(language=language):
                env = os.environ.copy()
                env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8", LANGUAGE=language)
                result = subprocess.run(["/usr/bin/python3", "-c", code], cwd=project,
                                        env=env, text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.splitlines(), expected)


if __name__ == "__main__":
    unittest.main()
