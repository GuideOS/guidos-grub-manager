import gettext
import unittest
from unittest import mock
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    import app
    import grub_manager
except ImportError:
    Gtk = None


@unittest.skipIf(Gtk is None, "GTK and PyGObject are unavailable")
class WindowOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest("No graphical display available")
        cls.application = app.Application()
        if not cls.application.register(None):
            raise unittest.SkipTest("GTK application could not be registered")

    def setUp(self):
        patcher = mock.patch.object(app, "tr", gettext.NullTranslations().gettext)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(grub_manager, "existing_configuration",
                               return_value=(["b.cfg", "a.cfg"], set(), None)), \
             mock.patch.object(grub_manager, "managed_windows_names", return_value=set()):
            self.window = app.Window(self.application)

    def tearDown(self):
        self.window.destroy()

    def test_german_catalog_is_used_for_ui_labels(self):
        project = Path(__file__).resolve().parent.parent
        translations = gettext.translation("gos-grub-manager", project / "locale",
                                           languages=["de"])
        with mock.patch.object(app, "tr", translations.gettext), \
             mock.patch.object(grub_manager, "existing_configuration",
                               return_value=([], set(), None)), \
             mock.patch.object(grub_manager, "managed_windows_names", return_value=set()):
            window = app.Window(self.application)
        try:
            self.assertEqual(window.scan_button.get_label(), "Suchen")
            self.assertEqual(window.save_button.get_label(), "In custom.cfg übernehmen")
        finally:
            window.destroy()

    def test_application_uses_user_color_scheme(self):
        style = Adw.StyleManager.get_default()
        previous = style.get_color_scheme()
        try:
            for scheme, expected in (("dark", Adw.ColorScheme.FORCE_DARK),
                                     ("light", Adw.ColorScheme.FORCE_LIGHT)):
                with self.subTest(scheme=scheme), \
                     mock.patch.dict(os.environ, {"GOS_USER_COLOR_SCHEME": scheme}), \
                     mock.patch.object(app, "Window"):
                    self.application.do_activate()
                    self.assertEqual(style.get_color_scheme(), expected)
                    self.assertEqual(style.get_dark(), scheme == "dark")
        finally:
            style.set_color_scheme(previous)

    def test_move_and_save_without_scan(self):
        self.assertEqual(self.window.order, ["b.cfg", "a.cfg"])
        self.assertTrue(self.window.save_button.get_sensitive())
        self.window.order_list.select_row(self.window.order_list.get_row_at_index(1))
        self.window.up_button.emit("clicked")
        self.assertEqual(self.window.order, ["a.cfg", "b.cfg"])
        with mock.patch.object(grub_manager, "install") as install:
            self.window.save_response(None, "cancel")
            install.assert_not_called()
            self.window.save_response(None, "save")
            install.assert_called_once_with([], order=["a.cfg", "b.cfg"],
                                            expected_existing=["b.cfg", "a.cfg"],
                                            disabled=set(), expected_disabled=set())

    def test_scan_appends_new_entries_without_duplicate_existing_files(self):
        configs = [
            grub_manager.FoundConfig("/dev/a", "boot/grub/grub.cfg", "A",
                                     "a.cfg", grub_manager.MARKER, 1),
            grub_manager.FoundConfig("/dev/c", "boot/grub/grub.cfg", "C",
                                     "c.cfg", grub_manager.MARKER, 1),
        ]
        self.window.scan_done(grub_manager.ScanResult(configs, []))
        self.assertEqual(self.window.order, ["b.cfg", "a.cfg", "c.cfg"])
        self.window.order_list.select_row(self.window.order_list.get_row_at_index(2))
        self.window.up_button.emit("clicked")
        with mock.patch.object(grub_manager, "install") as install:
            self.window.save_response(None, "save")
            install.assert_called_once_with(configs, order=["b.cfg", "c.cfg", "a.cfg"],
                                            expected_existing=["b.cfg", "a.cfg"],
                                            disabled=set(), expected_disabled=set())

    def test_unverified_windows_is_disabled_and_confirmed_one_is_reenabled(self):
        self.window.order = ["b.cfg", "windows-old.cfg", "windows-valid.cfg"]
        self.window.existing_names = set(self.window.order)
        self.window.saved_order = list(self.window.order)
        self.window.saved_disabled = {"windows-valid.cfg"}
        self.window.disabled = set(self.window.saved_disabled)
        self.window.managed_windows = {"windows-old.cfg", "windows-valid.cfg"}
        config = grub_manager.FoundConfig(
            "/dev/esp", "EFI/Microsoft/Boot/bootmgfw.efi", "Windows",
            "windows-valid.cfg", grub_manager.MARKER + grub_manager.WINDOWS_MARKER, 1)
        self.window.scan_done(grub_manager.ScanResult([config], []))
        self.assertEqual(self.window.disabled, {"windows-old.cfg"})
        with mock.patch.object(grub_manager, "install") as install:
            self.window.save_response(None, "save")
            install.assert_called_once_with([config], order=self.window.order,
                                            expected_existing=self.window.saved_order,
                                            disabled={"windows-old.cfg"},
                                            expected_disabled={"windows-valid.cfg"})

    def test_foreign_master_disables_save_but_lists_files(self):
        with mock.patch.object(grub_manager, "existing_configuration",
                               return_value=(["manual.cfg"], set(), "Unmanaged custom.cfg")), \
             mock.patch.object(grub_manager, "managed_windows_names", return_value=set()):
            window = app.Window(self.application)
        try:
            self.assertEqual(window.order, ["manual.cfg"])
            self.assertFalse(window.save_button.get_sensitive())
            self.assertIn("Unmanaged custom.cfg", window.status.get_text())
        finally:
            window.destroy()

    def test_double_click_opens_selected_existing_file_as_invoking_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom"
            custom.mkdir()
            (custom / "a.cfg").write_text("menuentry 'A' {}", encoding="utf-8")
            user = SimpleNamespace(pw_name="alice", pw_dir="/home/alice")
            self.window.order_list.select_row(self.window.order_list.get_row_at_index(1))
            self.window.up_button.emit("clicked")
            row = self.window.order_list.get_row_at_index(0)
            with mock.patch.object(grub_manager, "BOOT_GRUB", Path(tmp)), \
                 mock.patch.dict(os.environ, {"PKEXEC_UID": "1000",
                                               "GOS_USER_XDG_CONFIG_HOME": "/home/alice/config",
                                               "DBUS_SESSION_BUS_ADDRESS": "private",
                                               "GOS_USER_DBUS_SESSION_BUS_ADDRESS": "user-bus",
                                               "ADW_DISABLE_PORTAL": "1",
                                               "GOS_USER_ADW_DISABLE_PORTAL": "",
                                               "GDK_DEBUG": "no-portals",
                                               "GOS_USER_GDK_DEBUG": "events",
                                               "GIO_USE_VFS": "local",
                                               "GOS_USER_GIO_USE_VFS": ""}), \
                 mock.patch.object(app.pwd, "getpwuid", return_value=user), \
                 mock.patch.object(app.threading, "Thread") as thread:
                self.window.order_list.emit("row-activated", row)
            self.assertFalse(self.window.order_list.get_activate_on_single_click())
            self.assertEqual(thread.call_args.kwargs["args"][0:2],
                             ("alice", custom / "a.cfg"))
            self.assertEqual(thread.call_args.kwargs["args"][2]["HOME"], "/home/alice")
            self.assertEqual(thread.call_args.kwargs["args"][2]["XDG_CONFIG_HOME"],
                             "/home/alice/config")
            self.assertEqual(thread.call_args.kwargs["args"][2]["DBUS_SESSION_BUS_ADDRESS"],
                             "user-bus")
            self.assertEqual(thread.call_args.kwargs["args"][2]["GDK_DEBUG"], "events")
            self.assertNotIn("ADW_DISABLE_PORTAL", thread.call_args.kwargs["args"][2])
            self.assertNotIn("GIO_USE_VFS", thread.call_args.kwargs["args"][2])
            thread.return_value.start.assert_called_once()

    def test_unsaved_or_missing_file_is_not_opened(self):
        self.window.order.append("new.cfg")
        self.window.refresh_order()
        with mock.patch.object(app.threading, "Thread") as thread:
            self.window.order_list.emit("row-activated",
                                        self.window.order_list.get_row_at_index(2))
            self.assertIn("not been saved yet", self.window.status.get_text())
            with tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(grub_manager, "BOOT_GRUB", Path(tmp)):
                self.window.order_list.emit("row-activated",
                                            self.window.order_list.get_row_at_index(0))
            self.assertIn("File is missing", self.window.status.get_text())
            thread.assert_not_called()

    def test_refuses_editor_as_root_or_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom"
            custom.mkdir()
            path = custom / "b.cfg"
            path.write_text("test", encoding="utf-8")
            row = self.window.order_list.get_row_at_index(0)
            with mock.patch.object(grub_manager, "BOOT_GRUB", Path(tmp)), \
                 mock.patch.dict(os.environ, {"PKEXEC_UID": "0"}), \
                 mock.patch.object(app.threading, "Thread") as thread:
                self.window.order_list.emit("row-activated", row)
                self.assertIn("unprivileged user", self.window.status.get_text())
                path.unlink()
                path.symlink_to("/etc/passwd")
                self.window.order_list.emit("row-activated", row)
                self.assertIn("symbolic link", self.window.status.get_text())
                thread.assert_not_called()

    def test_editor_launch_failure_is_reported(self):
        failure = SimpleNamespace(returncode=1, stderr="No default editor")
        with mock.patch.object(app.subprocess, "run", return_value=failure) as run, \
             mock.patch.object(app.GLib, "idle_add") as idle:
            self.window.open_editor_worker("alice", Path("/tmp/a.cfg"), {"HOME": "/home/alice"})
        self.assertEqual(run.call_args.args[0][:3], ["runuser", "-u", "alice"])
        self.assertIn("No default editor", idle.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
