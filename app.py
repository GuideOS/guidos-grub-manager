#!/usr/bin/python3
"""GTK interface; privileged operations occur only after a user action."""

import os
from pathlib import Path
import pwd
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

import grub_manager
from i18n import _ as tr, ngettext


class Window(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="GOS GRUB Manager",
                         default_width=820, default_height=600)
        self.result = None
        self.scanning = False
        self.order = []
        self.existing_names = set()
        self.saved_order = []
        self.disabled = set()
        self.saved_disabled = set()
        self.managed_windows = set()
        self.load_error = None
        self.connect("close-request", self.close_request)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        header.set_decoration_layout(":minimize,maximize,close")
        layout.append(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        layout.append(box)
        box.set_vexpand(True)
        self.set_content(layout)
        heading = Gtk.Label(label=tr("Search for operating systems with GRUB"))
        heading.add_css_class("title-2")
        box.append(heading)
        info = Gtk.Label(
            label=tr("The scan reads GRUB configurations and checks Windows UEFI. "
                     "The running system's GRUB configuration is excluded. "
                     "Windows legacy BIOS is not supported. "
                     "Nothing is saved until you confirm."),
            wrap=True, xalign=0,
        )
        box.append(info)
        buttons = Gtk.Box(spacing=8)
        self.scan_button = Gtk.Button(label=tr("Search"))
        self.scan_button.connect("clicked", self.start_scan)
        buttons.append(self.scan_button)
        self.save_button = Gtk.Button(label=tr("Add to custom.cfg"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.confirm_save)
        buttons.append(self.save_button)
        box.append(buttons)
        self.status = Gtk.Label(label=tr("Ready."), xalign=0, wrap=True)
        box.append(self.status)
        order_heading = Gtk.Label(label=tr("Configuration file order"), xalign=0)
        order_heading.add_css_class("heading")
        box.append(order_heading)
        order_info = Gtk.Label(
            label=tr("Select a file and move it, or double-click to open it in the editor. "
                     "New search results are appended at the bottom."),
            wrap=True, xalign=0,
        )
        box.append(order_info)
        order_box = Gtk.Box(spacing=8)
        order_scroll = Gtk.ScrolledWindow(hexpand=True, min_content_height=120,
                                          max_content_height=180,
                                          propagate_natural_height=True)
        self.order_list = Gtk.ListBox()
        self.order_list.add_css_class("boxed-list")
        self.order_list.set_activate_on_single_click(False)
        self.order_list.connect("row-selected", self.row_selected)
        self.order_list.connect("row-activated", self.open_row)
        order_scroll.set_child(self.order_list)
        order_box.append(order_scroll)
        move_buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.up_button = Gtk.Button(label=tr("Move up"))
        self.up_button.connect("clicked", self.move_row, -1)
        move_buttons.append(self.up_button)
        self.down_button = Gtk.Button(label=tr("Move down"))
        self.down_button.connect("clicked", self.move_row, 1)
        move_buttons.append(self.down_button)
        order_box.append(move_buttons)
        box.append(order_box)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.output = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                   wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll.set_child(self.output)
        box.append(scroll)
        try:
            self.order, self.disabled, self.load_error = grub_manager.existing_configuration()
            self.existing_names = set(self.order)
            self.saved_order = list(self.order)
            self.saved_disabled = set(self.disabled)
            self.managed_windows = grub_manager.managed_windows_names()
        except (OSError, UnicodeError, ValueError) as exc:
            self.load_error = str(exc)
        if self.load_error:
            self.status.set_text(tr("Cannot save order: {error}").format(error=self.load_error))
        elif self.order:
            count = len(self.order)
            self.status.set_text(ngettext("{count} existing configuration loaded.",
                                          "{count} existing configurations loaded.", count).format(count=count))
        self.refresh_order()

    def update_buttons(self):
        row = self.order_list.get_selected_row()
        index = row.get_index() if row is not None else -1
        enabled = not self.scanning
        self.up_button.set_sensitive(enabled and index > 0)
        self.down_button.set_sensitive(enabled and 0 <= index < len(self.order) - 1)
        self.save_button.set_sensitive(enabled and not self.load_error and bool(self.order))

    def row_selected(self, _listbox, _row):
        self.update_buttons()

    def refresh_order(self, selected=-1):
        self.order_list.unselect_all()
        while (row := self.order_list.get_row_at_index(0)) is not None:
            self.order_list.remove(row)
        for name in self.order:
            suffix = tr(" (not included)") if name in self.disabled else (
                "" if name in self.existing_names else tr(" (new)")
            )
            label = Gtk.Label(label=name + suffix, xalign=0)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(12)
            label.set_margin_end(12)
            self.order_list.append(label)
        if selected >= 0:
            self.order_list.select_row(self.order_list.get_row_at_index(selected))
        self.update_buttons()

    def move_row(self, _button, offset):
        row = self.order_list.get_selected_row()
        if row is None:
            return
        index = row.get_index()
        target = index + offset
        if 0 <= target < len(self.order):
            self.order[index], self.order[target] = self.order[target], self.order[index]
            self.refresh_order(target)
            self.status.set_text(tr("Order changed; not saved yet."))

    def open_row(self, _listbox, row):
        index = row.get_index()
        if not 0 <= index < len(self.order):
            self.status.set_text(tr("The selected file is no longer in the list."))
            return
        name = self.order[index]
        if name not in self.existing_names:
            self.status.set_text(tr("This file has not been saved yet and cannot be opened."))
            return
        if not grub_manager.SAFE_NAME.fullmatch(name):
            self.status.set_text(tr("Invalid filename: {name}").format(name=name))
            return
        custom = grub_manager.BOOT_GRUB / "custom"
        path = custom / name
        if custom.is_symlink() or path.is_symlink() or not path.is_file():
            self.status.set_text(tr("File is missing or is a symbolic link: {name}").format(name=name))
            return
        try:
            uid = int(os.environ["PKEXEC_UID"])
            if uid <= 0:
                raise ValueError(tr("No unprivileged user is available."))
            user = pwd.getpwuid(uid)
        except (KeyError, ValueError) as exc:
            self.status.set_text(tr("Cannot launch editor: {error}").format(error=exc))
            return
        env = os.environ.copy()
        env.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name)
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_DATA_DIRS", "XDG_CURRENT_DESKTOP", "ADW_DISABLE_PORTAL",
                    "GDK_DEBUG", "GIO_USE_VFS"):
            value = env.pop("GOS_USER_" + key, "")
            if value:
                env[key] = value
            else:
                env.pop(key, None)
        self.status.set_text(tr("Opening editor..."))
        threading.Thread(target=self.open_editor_worker, args=(user.pw_name, path, env),
                         daemon=True).start()

    def open_editor_worker(self, username, path, env):
        helper = Path(__file__).with_name("open_editor.py")
        try:
            result = subprocess.run(
                ["runuser", "-u", username, "--", "/usr/bin/python3", str(helper), str(path)],
                env=env, text=True, capture_output=True, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or tr("The editor could not be launched."))
        except (OSError, RuntimeError) as exc:
            GLib.idle_add(self.editor_status, tr("Could not open editor: {error}").format(error=exc))
        else:
            GLib.idle_add(self.editor_status, tr("File passed to the default editor."))

    def editor_status(self, message):
        self.status.set_text(message)
        return GLib.SOURCE_REMOVE

    def start_scan(self, _button):
        self.result = None
        self.scanning = True
        self.order = [name for name in self.order if name in self.existing_names]
        self.scan_button.set_sensitive(False)
        self.refresh_order()
        self.status.set_text(tr("Searching; unmounted filesystems are temporarily mounted..."))
        self.output.get_buffer().set_text("")
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def scan_worker(self):
        try:
            result = grub_manager.scan()
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            GLib.idle_add(self.scan_failed, str(exc))
        else:
            GLib.idle_add(self.scan_done, result)

    def scan_failed(self, error):
        self.scanning = False
        self.status.set_text(tr("Search failed: {error}").format(error=error))
        self.scan_button.set_sensitive(True)
        self.update_buttons()
        return GLib.SOURCE_REMOVE

    def scan_done(self, result):
        self.scanning = False
        self.result = result
        self.order.extend(item.filename for item in result.configs
                          if item.filename not in self.order)
        verified_windows = {
            item.filename for item in result.configs
            if item.content.startswith(grub_manager.MARKER + grub_manager.WINDOWS_MARKER)
        }
        self.disabled.difference_update(verified_windows)
        self.disabled.update(self.managed_windows - verified_windows)
        lines = [
            ngettext("{name} — {device}:/{location}\n"
                     "  {count} boot entry → {filename}\n\n{content}",
                     "{name} — {device}:/{location}\n"
                     "  {count} boot entries → {filename}\n\n{content}",
                     item.entry_count).format(
                   name=item.name, device=item.device, location=item.location,
                   count=item.entry_count, filename=item.filename, content=item.content)
            for item in result.configs
        ]
        if result.warnings:
            lines.append("\n" + tr("Notes:") + "\n" + "\n".join(result.warnings))
        if self.managed_windows - verified_windows:
            lines.append("\n" + tr("Unverified Windows files will no longer be included when "
                                   "saving; the files will be retained:") + "\n"
                         + "\n".join(sorted(self.managed_windows - verified_windows)))
        self.output.get_buffer().set_text("\n\n".join(lines) or tr("No configuration found."))
        count = len(result.configs)
        status = ngettext("{count} configuration found.", "{count} configurations found.", count).format(count=count)
        if self.load_error:
            status += " " + tr("Cannot save order: {error}").format(error=self.load_error)
        self.status.set_text(status)
        self.scan_button.set_sensitive(True)
        self.refresh_order()
        return GLib.SOURCE_REMOVE

    def close_request(self, _window):
        if self.scanning:
            self.status.set_text(tr("Please wait until scanning and unmounting are complete."))
            return True
        return False

    def confirm_save(self, _button):
        if not self.order or self.load_error:
            return
        dialog = Adw.MessageDialog.new(
            self, tr("Save GRUB entries?"),
            tr("Files under /boot/grub/custom and the managed /boot/grub/custom.cfg "
               "will be created or updated using the displayed order. "
               "Existing unmanaged custom.cfg files will not be overwritten. "
               "Only import trusted disks. Search for GRUB entries again after kernel updates. "
               "Windows entries are for UEFI only; verification does not guarantee bootability. "
               "Unverified managed Windows files will not be included, but will be retained."),
        )
        dialog.add_response("cancel", tr("Cancel"))
        dialog.add_response("save", tr("Save"))
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self.save_response)
        dialog.present()

    def save_response(self, _dialog, response):
        if response != "save" or not self.order or self.load_error:
            return
        try:
            grub_manager.install(self.result.configs if self.result else [], order=self.order,
                                 expected_existing=self.saved_order, disabled=self.disabled,
                                 expected_disabled=self.saved_disabled)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            self.status.set_text(tr("Save failed: {error}").format(error=exc))
        else:
            self.existing_names = set(self.order)
            self.saved_order = list(self.order)
            self.saved_disabled = set(self.disabled)
            if self.result:
                self.managed_windows.update(
                    item.filename for item in self.result.configs
                    if item.content.startswith(grub_manager.MARKER + grub_manager.WINDOWS_MARKER)
                )
            self.result = None
            self.refresh_order()
            self.status.set_text(tr("Configuration saved. grub.cfg was not changed."))


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.gos.GrubManager",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        scheme = os.environ.get("GOS_USER_COLOR_SCHEME")
        if scheme in ("dark", "light"):
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT
            )
        Window(self).present()


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit(tr("Please start with /usr/bin/python3 start.py and pkexec."))
    Application().run(None)
