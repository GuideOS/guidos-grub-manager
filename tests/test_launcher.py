import gettext
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import start


class LauncherTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(start, "_", gettext.NullTranslations().gettext)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runs_manager_in_private_bus_and_preserves_user_bus_for_editor(self):
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            pkexec = Path(tmp) / "pkexec"
            pkexec.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            pkexec.chmod(0o755)
            env = os.environ.copy()
            env.update(LANG="C", LC_ALL="C", LANGUAGE="C")
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env["DBUS_SESSION_BUS_ADDRESS"] = "user-bus"
            env["AT_SPI_BUS_ADDRESS"] = "user-accessibility-bus"
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            result = subprocess.run([str(project / "start.py")], env=env, text=True,
                                    capture_output=True, check=True)
        args = result.stdout.splitlines()
        self.assertIn("GOS_USER_DBUS_SESSION_BUS_ADDRESS=user-bus", args)
        self.assertIn("AT_SPI_BUS_ADDRESS=user-accessibility-bus", args)
        self.assertIn("ADW_DISABLE_PORTAL=1", args)
        self.assertIn("GDK_DEBUG=no-portals", args)
        self.assertIn("GIO_USE_VFS=local", args)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS=user-bus", args)
        self.assertEqual(args[:3], ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR"])
        self.assertEqual(args[-6:], ["dbus-run-session", "--", "/usr/bin/env",
                                     "XDG_RUNTIME_DIR=/run/user/1000", "/usr/bin/python3",
                                     str(project / "app.py")])

    def test_missing_user_accessibility_bus_is_reported_without_blocking_start(self):
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            pkexec = Path(tmp) / "pkexec"
            pkexec.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            pkexec.chmod(0o755)
            env = os.environ.copy()
            env.update(LANG="C", LC_ALL="C", LANGUAGE="C")
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env.pop("AT_SPI_BUS_ADDRESS", None)
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + tmp + "/missing"
            result = subprocess.run(["/usr/bin/python3", str(project / "start.py")], env=env, text=True,
                                    capture_output=True, check=True, timeout=15)
        self.assertIn("user session accessibility bus is unavailable",
                      result.stderr)
        self.assertIn("AT_SPI_BUS_ADDRESS=", result.stdout.splitlines())

    def test_bus_does_not_inherit_runtime_directory_owned_by_another_user(self):
        if os.getuid() == Path("/run").stat().st_uid or not shutil.which("dbus-run-session"):
            self.skipTest("No foreign runtime directory or dbus-run-session available")
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            pkexec = Path(tmp) / "pkexec"
            pkexec.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            pkexec.chmod(0o755)
            env = os.environ.copy()
            env.update(LANG="C", LC_ALL="C", LANGUAGE="C")
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env["AT_SPI_BUS_ADDRESS"] = "unused"
            env["XDG_RUNTIME_DIR"] = "/run"
            launcher = subprocess.run(["/usr/bin/python3", str(project / "start.py")], env=env, text=True,
                                      capture_output=True, check=True, timeout=15)
            args = launcher.stdout.splitlines()
            args[-2:] = ["/usr/bin/python3", "-c",
                         "import os; from gi.repository import Gio; "
                         "assert os.environ['XDG_RUNTIME_DIR'] == '/run'; "
                         "Gio.bus_get_sync(Gio.BusType.SESSION, None)"]
            result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Unable to set up transient service directory", result.stderr)

    def test_manager_gtk_does_not_activate_desktop_portal(self):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("No graphical display available")
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not shutil.which("dbus-run-session"):
            self.skipTest("No session bus available")
        project = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            pkexec = Path(tmp) / "pkexec"
            pkexec.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            pkexec.chmod(0o755)
            env = os.environ.copy()
            env.update(LANG="C", LC_ALL="C", LANGUAGE="C")
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env.pop("AT_SPI_BUS_ADDRESS", None)
            launcher = subprocess.run(["/usr/bin/python3", str(project / "start.py")], env=env, text=True,
                                      capture_output=True, check=True, timeout=15)
            if "accessibility bus" in launcher.stderr:
                self.skipTest("No accessibility bus available")
            args = launcher.stdout.splitlines()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["G_DEBUG"] = "fatal-criticals"
            code = """
import sys
from unittest import mock
import app
from gi.repository import Gio, GLib, Gtk
if not Gtk.init_check():
    sys.exit(77)
application = app.Application()
assert application.register(None)
with mock.patch.object(app.grub_manager, 'existing_configuration', return_value=([], set(), None)), \\
     mock.patch.object(app.grub_manager, 'managed_windows_names', return_value=set()):
    window = app.Window(application)
while GLib.MainContext.default().pending():
    GLib.MainContext.default().iteration(False)
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
reply = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                      'org.freedesktop.DBus', 'NameHasOwner',
                      GLib.Variant('(s)', ('org.freedesktop.portal.Desktop',)),
                      GLib.VariantType.new('(b)'), Gio.DBusCallFlags.NONE, 5000, None)
assert not reply.unpack()[0], 'Desktop portal was activated'
window.destroy()
"""
            args[-2:] = ["/usr/bin/python3", "-c", code]
            result = subprocess.run(args, cwd=project, env=env, text=True,
                                    capture_output=True, timeout=30)
        if result.returncode == 77:
            self.skipTest("GTK cannot open the display")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Activating service name='org.freedesktop.portal.Desktop'", result.stderr)

    def test_address_is_discovered_from_user_session_bus(self):
        bus = mock.Mock()
        bus.call_sync.return_value.unpack.return_value = ("discovered-bus",)
        gio = SimpleNamespace(
            bus_get_sync=mock.Mock(return_value=bus),
            BusType=SimpleNamespace(SESSION=1),
            DBusCallFlags=SimpleNamespace(NONE=0),
        )
        glib = SimpleNamespace(VariantType=SimpleNamespace(new=lambda value: value),
                               Error=RuntimeError)
        with mock.patch.dict(os.environ, {"AT_SPI_BUS_ADDRESS": ""}), \
             mock.patch.dict("sys.modules", {"gi.repository": SimpleNamespace(Gio=gio, GLib=glib)}):
            self.assertEqual(start.accessibility_address(), "discovered-bus")
        bus.call_sync.assert_called_once_with(
            "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
            "GetAddress", None, "(s)", 0, 5000, None)

    def test_color_scheme_uses_portal_preference_and_gsettings_fallback(self):
        bus = mock.Mock()
        schema = mock.Mock()
        schema.has_key.side_effect = lambda key: key in ("color-scheme", "gtk-theme")
        settings = mock.Mock()
        settings.get_string.side_effect = lambda key: {
            "color-scheme": "prefer-dark", "gtk-theme": "Adwaita",
        }[key]
        gio = SimpleNamespace(
            bus_get_sync=mock.Mock(return_value=bus),
            BusType=SimpleNamespace(SESSION=1),
            DBusCallFlags=SimpleNamespace(NONE=0),
            SettingsSchemaSource=SimpleNamespace(
                get_default=lambda: SimpleNamespace(lookup=lambda _name, _recursive: schema)),
            Settings=SimpleNamespace(new=lambda _name: settings),
        )
        glib = SimpleNamespace(
            Variant=lambda _signature, value: value,
            VariantType=SimpleNamespace(new=lambda value: value),
            Error=RuntimeError,
        )
        with mock.patch.dict("sys.modules", {"gi.repository": SimpleNamespace(Gio=gio, GLib=glib)}):
            for portal_value, expected in ((1, "dark"), (2, "light"), (0, "dark")):
                with self.subTest(portal_value=portal_value):
                    bus.call_sync.return_value.unpack.return_value = (portal_value,)
                    self.assertEqual(start.color_scheme(), expected)
            bus.call_sync.side_effect = RuntimeError("portal unavailable")
            self.assertEqual(start.color_scheme(), "dark")

            settings.get_string.side_effect = lambda key: {
                "color-scheme": "default", "gtk-theme": "Adwaita-dark",
            }[key]
            self.assertEqual(start.color_scheme(), "dark")
            settings.get_string.side_effect = lambda key: {
                "color-scheme": "default", "gtk-theme": "Adwaita",
            }[key]
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(start.color_scheme(), "")
                self.assertIn("portal unavailable", stderr.getvalue())

    def test_color_scheme_is_forwarded_to_privileged_process(self):
        with mock.patch.object(start, "color_scheme", return_value="dark"), \
             mock.patch.object(start, "accessibility_address", return_value="user-bus"), \
             mock.patch.object(start.os, "execvp") as execute:
            start.main()
        self.assertIn("GOS_USER_COLOR_SCHEME=dark", execute.call_args.args[1])

    def test_pkexec_launch_failure_is_reported(self):
        with mock.patch.object(start, "accessibility_address", return_value="user-bus"), \
             mock.patch.object(start, "color_scheme", return_value=""), \
             mock.patch.object(start.os, "execvp", side_effect=FileNotFoundError("pkexec")):
            with self.assertRaisesRegex(SystemExit, "Launch failed: pkexec"):
                start.main()

    def test_user_locale_is_forwarded_to_privileged_process(self):
        locale = {"LANG": "de_DE.UTF-8", "LANGUAGE": "de:en",
                  "LC_ALL": "de_DE.UTF-8", "LC_MESSAGES": "de_DE.UTF-8"}
        with mock.patch.dict(os.environ, locale, clear=True), \
             mock.patch.object(start, "accessibility_address", return_value="user-bus"), \
             mock.patch.object(start, "color_scheme", return_value=""), \
             mock.patch.object(start.os, "execvp") as execute:
            start.main()
        args = execute.call_args.args[1]
        for key, value in locale.items():
            self.assertIn(f"{key}={value}", args)
        project = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["/usr/bin/env", "-i", *args[1:-6], "/usr/bin/python3", "-c",
             "import i18n; print(i18n._('Search'))"],
            cwd=project, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "Suchen")


if __name__ == "__main__":
    unittest.main()
