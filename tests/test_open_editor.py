import gettext
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import open_editor
except ImportError:
    open_editor = None


@unittest.skipIf(open_editor is None, "PyGObject is unavailable")
class OpenEditorTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(open_editor, "_", gettext.NullTranslations().gettext)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_uses_default_text_editor_for_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.cfg"
            path.write_text("test", encoding="utf-8")
            editor = mock.Mock()
            editor.launch.return_value = True
            with mock.patch.object(open_editor.Gio.AppInfo, "get_default_for_type",
                                   return_value=editor) as get_default:
                open_editor.main(str(path))
            get_default.assert_called_once_with("text/plain", False)
            files, _ = editor.launch.call_args.args
            self.assertEqual(files[0].get_path(), str(path))

    def test_reports_missing_default_editor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.cfg"
            path.write_text("test", encoding="utf-8")
            with mock.patch.object(open_editor.Gio.AppInfo, "get_default_for_type",
                                   return_value=None):
                with self.assertRaisesRegex(RuntimeError, "No default editor"):
                    open_editor.main(str(path))

    def test_reports_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PermissionError):
                open_editor.main(str(Path(tmp) / "missing.cfg"))


if __name__ == "__main__":
    unittest.main()
