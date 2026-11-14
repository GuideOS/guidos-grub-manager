#!/usr/bin/python3
"""Start the manager with a private session bus and administrator privileges."""

import os
from pathlib import Path
import sys

from i18n import _


def accessibility_address() -> str:
    address = os.environ.get("AT_SPI_BUS_ADDRESS", "")
    if address:
        return address
    try:
        from gi.repository import Gio, GLib
    except ImportError as exc:
        print(_("Note: The user session accessibility bus is unavailable: {error}").format(error=exc),
              file=sys.stderr)
    else:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            result = bus.call_sync(
                "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
                "GetAddress", None, GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE, 5000, None,
            )
            address = result.unpack()[0]
        except GLib.Error as exc:
            print(_("Note: The user session accessibility bus is unavailable: {error}").format(error=exc),
                  file=sys.stderr)
    if not address:
        print(_("Note: GTK may not be able to provide accessibility support."),
              file=sys.stderr)
    return address


def color_scheme() -> str:
    try:
        from gi.repository import Gio, GLib
    except ImportError as exc:
        print(_("Note: Could not determine the user color scheme: {error}").format(error=exc),
              file=sys.stderr)
        return ""

    portal_error = None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings", "Read",
            GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
            GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
        )
        value = result.unpack()[0]
        if type(value) is int and value in (1, 2):
            return "dark" if value == 1 else "light"
    except GLib.Error as exc:
        portal_error = exc

    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.desktop.interface", True) if source else None
    if schema:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        if schema.has_key("color-scheme"):
            preference = settings.get_string("color-scheme")
            if preference == "prefer-dark":
                return "dark"
            if preference == "prefer-light":
                return "light"
        if schema.has_key("gtk-theme") and "dark" in settings.get_string("gtk-theme").casefold():
            return "dark"
    if portal_error is not None:
        print(_("Note: Could not determine the user color scheme: {error}").format(error=portal_error),
              file=sys.stderr)
    return ""


def main() -> None:
    env = os.environ
    user_variables = (
        "DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_DATA_DIRS", "XDG_CURRENT_DESKTOP", "ADW_DISABLE_PORTAL",
        "GDK_DEBUG", "GIO_USE_VFS",
    )
    args = [
        "pkexec", "/usr/bin/env", "-u", "XDG_RUNTIME_DIR",
        *(f"{name}={env[name]}" for name in ("LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES")
          if name in env),
        f"DISPLAY={env.get('DISPLAY', '')}",
        f"XAUTHORITY={env.get('XAUTHORITY', (env.get('HOME') or '/root') + '/.Xauthority')}",
        f"WAYLAND_DISPLAY={env.get('WAYLAND_DISPLAY', '')}",
        f"AT_SPI_BUS_ADDRESS={accessibility_address()}",
        f"GOS_USER_COLOR_SCHEME={color_scheme()}",
        *(f"GOS_USER_{name}={env.get(name, '')}" for name in user_variables),
        "ADW_DISABLE_PORTAL=1", "GDK_DEBUG=no-portals", "GIO_USE_VFS=local",
        "dbus-run-session", "--", "/usr/bin/env",
        f"XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR', '')}",
        "/usr/bin/python3", str(Path(__file__).resolve().with_name("app.py")),
    ]
    try:
        os.execvp(args[0], args)
    except OSError as exc:
        raise SystemExit(_("Launch failed: {error}").format(error=exc)) from exc


if __name__ == "__main__":
    main()
