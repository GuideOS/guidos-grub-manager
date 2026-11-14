#!/usr/bin/python3
"""Open a file in the user's default text editor after dropping privileges."""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from i18n import _


def main(path):
    if not os.access(path, os.R_OK):
        raise PermissionError(_("The file is not readable by the logged-in user."))
    editor = Gio.AppInfo.get_default_for_type("text/plain", False)
    if editor is None:
        raise RuntimeError(_("No default editor for text files is configured."))
    if not editor.launch([Gio.File.new_for_path(path)], None):
        raise RuntimeError(_("The default editor could not be launched."))


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (OSError, RuntimeError, GLib.Error) as exc:
        raise SystemExit(str(exc))
