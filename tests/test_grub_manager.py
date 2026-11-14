import json
import gettext
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import grub_manager as manager


def setUpModule():
    patcher = mock.patch.object(manager, "tr", gettext.NullTranslations().gettext)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


CONFIG = """\
function load_video {
  if [ x$grub_platform = xefi ]; then
    insmod efi_gop
  fi
}
menuentry 'Debian GNU/Linux' --class debian $menuentry_id_option 'debian' {
  load_video
  search --no-floppy --fs-uuid --set=root 1234-ABCD
  linux /vmlinuz root=UUID=abcd quiet splash
  initrd /microcode.img /initrd.img
}
submenu 'Advanced options' {
  menuentry 'Debian recovery' {
    linux /vmlinuz root=UUID=abcd single
    initrd /initrd.img
  }
}
menuentry 'Firmware' { fwsetup }
"""


class ExtractionTests(unittest.TestCase):
    def test_extracts_entries_and_preserves_boot_arguments_and_helpers(self):
        content, name, count, warnings = manager.extract_entries(CONFIG, "1234-ABCD")
        self.assertEqual((name, count, warnings), ("Debian GNU/Linux", 2, []))
        self.assertIn("root=UUID=abcd quiet splash", content)
        self.assertIn("initrd /microcode.img /initrd.img", content)
        self.assertIn("search --no-floppy --fs-uuid --set=root 1234-ABCD", content)
        self.assertIn("if search --no-floppy", content)
        self.assertIn(" --id 'debian'", content)
        self.assertNotIn("menuentry 'Firmware'", content)
        self.assertRegex(content, r"function gos_[a-f0-9]+_load_video")
        self.assertRegex(content, r"\n\s+gos_[a-f0-9]+_load_video\n")
        manager._validate(content)

    def test_unresolved_variables_are_reported_not_guessed(self):
        text = "menuentry 'Ubuntu' {\n linux /vmlinuz quiet splash $vt_handoff\n}\n"
        content, name, count, warnings = manager.extract_entries(text, "ABCD-1234")
        self.assertEqual(count, 0)
        self.assertEqual(name, "")
        self.assertIn("vt_handoff", warnings[0])

    def test_braces_in_strings_and_variable_expansions(self):
        text = """menuentry 'Linux {safe}' {
  set args="${root}"
  linux /vmlinuz root=UUID=123
}
"""
        content, _, count, _ = manager.extract_entries(text, "ABCD")
        self.assertEqual(count, 1)
        self.assertIn('set args="${root}"', content)
        manager._validate(content)

    def test_invalid_uuid_and_malformed_block_fail(self):
        with self.assertRaises(ValueError):
            manager.extract_entries(CONFIG, "abc; evil")
        with self.assertRaises(ValueError):
            manager.extract_entries("menuentry 'Broken' {\n linux /vmlinuz\n", "abcd")

    def test_windows_chainloader_is_not_copied_without_bcd_verification(self):
        config = """menuentry 'Windows Boot Manager' {
  chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}
menuentry 'Linux' {
  linux /vmlinuz
}
"""
        content, _, count, warnings = manager.extract_entries(config, "abcd")
        self.assertEqual(count, 1)
        self.assertNotIn("bootmgfw", content)
        self.assertIn("BCD", warnings[0])


class InstallTests(unittest.TestCase):
    def config(self, name="debian-123.cfg"):
        return manager.FoundConfig("/dev/test", "boot/grub/grub.cfg", "Debian", name,
                                   manager.MARKER + "menuentry 'Debian' { linux /vmlinuz; }\n", 1)

    def test_installs_sources_for_all_configs_without_touching_grub_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "grub.cfg").write_text("unchanged", encoding="utf-8")
            (boot / "custom").mkdir()
            (boot / "custom" / "manual.cfg").write_text(
                "menuentry 'Manual' { linux /manual; }\n", encoding="utf-8"
            )
            manager.install([self.config()], boot)
            self.assertEqual((boot / "grub.cfg").read_text(), "unchanged")
            self.assertEqual((boot / "custom.cfg").read_text(), manager.MARKER +
                             'source "${prefix}/custom/manual.cfg"\n'
                             'source "${prefix}/custom/debian-123.cfg"\n')
            self.assertTrue((boot / "custom" / "debian-123.cfg").exists())

    def test_refuses_foreign_master_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            master = boot / "custom.cfg"
            master.write_text("manual", encoding="utf-8")
            with self.assertRaises(ValueError):
                manager.install([self.config()], boot)
            self.assertEqual(master.read_text(), "manual")
            master.unlink()
            (boot / "custom").mkdir()
            target = boot / "custom" / "debian-123.cfg"
            target.write_text("manual", encoding="utf-8")
            with self.assertRaises(ValueError):
                manager.install([self.config()], boot)
            self.assertEqual(target.read_text(), "manual")

    def test_write_failure_rolls_back_previous_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            target = boot / "custom" / "debian-123.cfg"
            target.write_text(manager.MARKER + "old", encoding="utf-8")
            original_write = manager._atomic_write

            def failing_write(path, content):
                if path.name == "custom.cfg":
                    raise OSError("disk full")
                original_write(path, content)

            with mock.patch.object(manager, "_atomic_write", side_effect=failing_write):
                with self.assertRaises(OSError):
                    manager.install([self.config()], boot)
            self.assertEqual(target.read_text(), manager.MARKER + "old")
            self.assertFalse((boot / "custom.cfg").exists())

    def test_rejects_bad_syntax_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            bad = manager.FoundConfig("/dev/test", "boot/grub/grub.cfg", "Bad",
                                      "bad.cfg", manager.MARKER + "menuentry 'bad' {\n", 1)
            with self.assertRaises(RuntimeError):
                manager.install([bad], boot)
            self.assertFalse((boot / "custom").exists())

    def test_existing_order_can_be_saved_without_scan_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            custom = boot / "custom"
            custom.mkdir()
            for name in ("a.cfg", "b.cfg", "c.cfg"):
                (custom / name).write_text("menuentry 'Linux' { linux /vmlinuz; }\n",
                                           encoding="utf-8")
            master = boot / "custom.cfg"
            master.write_text(manager.MARKER +
                              'source "${prefix}/custom/b.cfg"\n'
                              'source "${prefix}/custom/a.cfg"\n', encoding="utf-8")
            self.assertEqual(manager.existing_order(boot), (["b.cfg", "a.cfg", "c.cfg"], None))
            manager.install([], boot, order=["c.cfg", "b.cfg", "a.cfg"])
            self.assertEqual(manager.existing_order(boot), (["c.cfg", "b.cfg", "a.cfg"], None))
            self.assertEqual(master.read_text(), manager.MARKER +
                             'source "${prefix}/custom/c.cfg"\n'
                             'source "${prefix}/custom/b.cfg"\n'
                             'source "${prefix}/custom/a.cfg"\n')

    def test_scan_appends_new_configs_without_resetting_existing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            for name in ("a.cfg", "b.cfg"):
                (boot / "custom" / name).write_text(
                    "menuentry 'Linux' { linux /vmlinuz; }\n", encoding="utf-8")
            (boot / "custom.cfg").write_text(
                manager.MARKER + 'source "${prefix}/custom/b.cfg"\n'
                'source "${prefix}/custom/a.cfg"\n', encoding="utf-8")
            manager.install([self.config()], boot)
            self.assertEqual(manager.existing_order(boot)[0],
                             ["b.cfg", "a.cfg", "debian-123.cfg"])

    def test_rejects_stale_or_duplicate_order_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            for name in ("a.cfg", "b.cfg"):
                (boot / "custom" / name).write_text(
                    "menuentry 'Linux' { linux /vmlinuz; }\n", encoding="utf-8")
            for order in (["a.cfg"], ["a.cfg", "a.cfg"], ["a.cfg", "missing.cfg"]):
                with self.subTest(order=order), self.assertRaises(ValueError):
                    manager.install([], boot, order=order)
                self.assertFalse((boot / "custom.cfg").exists())

    def test_nonstandard_or_foreign_master_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            (boot / "custom" / "a.cfg").write_text(
                "menuentry 'Linux' { linux /vmlinuz; }\n", encoding="utf-8")
            master = boot / "custom.cfg"
            for content in ("manual\n", manager.MARKER + "set timeout=3\n",
                            manager.MARKER + 'source "${prefix}/custom/missing.cfg"\n',
                            manager.MARKER + 2 * 'source "${prefix}/custom/a.cfg"\n'):
                with self.subTest(content=content):
                    master.write_text(content, encoding="utf-8")
                    order, warning = manager.existing_order(boot)
                    self.assertEqual(order, ["a.cfg"])
                    self.assertIsNotNone(warning)
                    with self.assertRaises(ValueError):
                        manager.install([], boot, order=order)
                    self.assertEqual(master.read_text(), content)

    def test_external_reordering_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            for name in ("a.cfg", "b.cfg"):
                (boot / "custom" / name).write_text(
                    "menuentry 'Linux' { linux /vmlinuz; }\n", encoding="utf-8")
            master = boot / "custom.cfg"
            original = (manager.MARKER + 'source "${prefix}/custom/b.cfg"\n'
                        'source "${prefix}/custom/a.cfg"\n')
            master.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed"):
                manager.install([], boot, order=["a.cfg", "b.cfg"],
                                expected_existing=["a.cfg", "b.cfg"])
            self.assertEqual(master.read_text(), original)

    def test_disabled_windows_config_is_preserved_and_remains_unlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            custom = boot / "custom"
            custom.mkdir()
            windows = custom / "windows-abc.cfg"
            content = manager.MARKER + manager.WINDOWS_MARKER + "invalid syntax {\n"
            windows.write_text(content, encoding="utf-8")
            (custom / "linux.cfg").write_text("menuentry 'Linux' { linux /vmlinuz; }\n",
                                              encoding="utf-8")
            manager.install([], boot, order=["windows-abc.cfg", "linux.cfg"],
                            disabled={"windows-abc.cfg"})
            self.assertEqual(windows.read_text(), content)
            self.assertEqual(manager.existing_configuration(boot),
                             (["windows-abc.cfg", "linux.cfg"], {"windows-abc.cfg"}, None))
            self.assertNotIn('source "${prefix}/custom/windows-abc.cfg"',
                             (boot / "custom.cfg").read_text())
            manager.install([], boot)
            self.assertEqual(manager.existing_configuration(boot)[1], {"windows-abc.cfg"})

    def test_manual_configs_cannot_be_disabled_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            (boot / "custom" / "windows-manual.cfg").write_text(
                "menuentry 'Manual' { linux /vmlinuz; }\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "managed Windows"):
                manager.install([], boot, disabled={"windows-manual.cfg"})
            self.assertFalse((boot / "custom.cfg").exists())

    def test_confirmed_windows_can_be_reenabled_in_its_original_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "custom").mkdir()
            windows = boot / "custom" / "windows-test.cfg"
            windows.write_text(manager.MARKER + manager.WINDOWS_MARKER +
                               "menuentry 'Old' { chainloader /old; }\n", encoding="utf-8")
            (boot / "custom" / "manual.cfg").write_text(
                "menuentry 'Manual' { linux /vmlinuz; }\n", encoding="utf-8")
            order = ["windows-test.cfg", "manual.cfg"]
            manager.install([], boot, order=order, disabled={"windows-test.cfg"})
            config = manager.FoundConfig(
                "/dev/esp", "EFI", "Windows", "windows-test.cfg",
                manager.MARKER + manager.WINDOWS_MARKER +
                "menuentry 'Windows' { chainloader /EFI/bootmgfw.efi; }\n", 1)
            manager.install([config], boot, order=order, disabled=set(),
                            expected_existing=order, expected_disabled={"windows-test.cfg"})
            self.assertEqual(manager.existing_configuration(boot), (order, set(), None))
            self.assertEqual(windows.read_text(), config.content)
            self.assertIn('source "${prefix}/custom/windows-test.cfg"', (boot / "custom.cfg").read_text())


class ScanTests(unittest.TestCase):
    def test_scans_grub_config_on_separate_boot_partition(self):
        for already_mounted in (False, True):
            with self.subTest(already_mounted=already_mounted), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "boot"
                config = root / "grub/grub.cfg"
                config.parent.mkdir(parents=True)
                config.write_text(CONFIG, encoding="utf-8")
                data = json.dumps({"blockdevices": [{
                    "path": "/dev/boot", "fstype": "vfat", "uuid": "BOOT-UUID",
                    "mountpoints": [str(root)] if already_mounted else [],
                }]})
                calls = []

                def fake_command(*args):
                    calls.append(args)
                    if args[0] == "lsblk":
                        return data
                    if args[0] == "findmnt":
                        return "BOOT-UUID\n" if args[3] == "UUID" else "vfat\n"
                    if args[0] in ("mount", "umount"):
                        return ""
                    self.fail(f"Unexpected command: {args}")

                with mock.patch.object(manager, "command", side_effect=fake_command), \
                     mock.patch.object(manager.tempfile, "mkdtemp", return_value=str(root)), \
                     mock.patch.object(Path, "rmdir"):
                    result = manager.scan()
                self.assertEqual(result.warnings, [])
                self.assertEqual(len(result.configs), 1)
                found = result.configs[0]
                self.assertEqual((found.device, found.location), ("/dev/boot", "grub/grub.cfg"))
                self.assertIn("search --no-floppy --fs-uuid --set=root BOOT-UUID", found.content)
                self.assertIn("insmod fat", found.content)
                self.assertEqual(sum(call[0] == "mount" for call in calls), 0 if already_mounted else 1)
                self.assertEqual(sum(call[0] == "umount" for call in calls), 0 if already_mounted else 1)

    def test_btrfs_mount_options_and_fallback(self):
        for modern_fails in (False, True):
            with self.subTest(modern_fails=modern_fails), tempfile.TemporaryDirectory() as tmp:
                mountpoint = Path(tmp) / "mount"
                mountpoint.mkdir()
                config = mountpoint / "@/boot/grub/grub.cfg"
                config.parent.mkdir(parents=True)
                config.write_text(CONFIG, encoding="utf-8")
                data = json.dumps({"blockdevices": [{
                    "path": "/dev/test", "fstype": "btrfs", "uuid": "ABCD",
                    "mountpoints": [],
                }]})
                calls = []

                def fake_command(*args):
                    calls.append(args)
                    if args[0] == "lsblk":
                        return data
                    if args[0] == "mount" and modern_fails and "rescue=nologreplay" in args[4]:
                        raise RuntimeError("unsupported rescue option")
                    if args[0] == "findmnt":
                        return "ABCD\n" if args[3] == "UUID" else "btrfs\n"
                    return ""

                with mock.patch.object(manager, "command", side_effect=fake_command), \
                     mock.patch.object(manager.tempfile, "mkdtemp", return_value=str(mountpoint)), \
                     mock.patch.object(Path, "rmdir"):
                    result = manager.scan()
                mounts = [call for call in calls if call[0] == "mount"]
                self.assertEqual(len(result.configs), 1)
                self.assertEqual(result.warnings, [])
                self.assertEqual(mounts[0][4], "ro,rescue=nologreplay,subvolid=5,nosuid,nodev,noexec")
                self.assertEqual(len(mounts), 2 if modern_fails else 1)
                if modern_fails:
                    self.assertEqual(mounts[1][4], "ro,nologreplay,subvolid=5,nosuid,nodev,noexec")
                self.assertEqual(sum(call[0] == "umount" for call in calls), 1)

    def test_btrfs_mount_reports_both_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            mountpoint = Path(tmp) / "mount"
            mountpoint.mkdir()
            data = json.dumps({"blockdevices": [{
                "path": "/dev/test", "fstype": "btrfs", "uuid": "ABCD",
                "mountpoints": [],
            }]})

            def fake_command(*args):
                if args[0] == "lsblk":
                    return data
                if args[0] == "mount":
                    if "rescue=nologreplay" in args[4]:
                        raise RuntimeError("modern mount failed")
                    raise RuntimeError("legacy mount failed")
                self.fail(f"Unexpected command: {args}")

            with mock.patch.object(manager, "command", side_effect=fake_command), \
                 mock.patch.object(manager.tempfile, "mkdtemp", return_value=str(mountpoint)):
                result = manager.scan()
            self.assertEqual(result.configs, [])
            self.assertEqual(len(result.warnings), 1)
            self.assertIn("modern mount failed", result.warnings[0])
            self.assertIn("legacy mount failed", result.warnings[0])
            self.assertFalse(mountpoint.exists())

    def test_running_system_config_is_not_read_even_through_another_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / "host/grub.cfg"
            host.parent.mkdir()
            host.write_text("menuentry 'Host' {\n linux /host\n}\n", encoding="utf-8")
            alias = root / "disk/boot/grub/grub.cfg"
            alias.parent.mkdir(parents=True)
            alias.hardlink_to(host)
            boot_alias = root / "disk/grub/grub.cfg"
            boot_alias.parent.mkdir(parents=True)
            boot_alias.hardlink_to(host)
            other = root / "disk/@/boot/grub/grub.cfg"
            other.parent.mkdir(parents=True)
            other.write_text("menuentry 'Other' {\n linux /other\n}\n", encoding="utf-8")
            data = json.dumps({"blockdevices": [{
                "path": "/dev/test", "fstype": "ext4", "uuid": "ABCD",
                "mountpoints": [str(root / "disk")],
            }]})

            def fake_command(*args):
                if args[0] == "lsblk":
                    return data
                if args[0] == "findmnt":
                    self.assertEqual(args[-1], str(other))
                    return "ABCD\n" if args[3] == "UUID" else "ext4\n"
                self.fail(f"Unexpected command: {args}")

            with mock.patch.object(manager, "BOOT_GRUB", host.parent), \
                 mock.patch.object(manager, "command", side_effect=fake_command):
                result = manager.scan()
            self.assertEqual([config.name for config in result.configs], ["Other"])
            self.assertEqual(result.warnings, [])

    def test_unmounted_filesystem_is_unmounted_after_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            mountpoint = Path(tmp) / "mount"
            mountpoint.mkdir()
            config = mountpoint / "boot" / "grub" / "grub.cfg"
            config.parent.mkdir(parents=True)
            config.write_text(CONFIG, encoding="utf-8")
            device_data = json.dumps({"blockdevices": [{
                "path": "/dev/test", "fstype": "ext4", "uuid": "ABCD",
                "mountpoints": [],
            }]})
            calls = []

            def fake_command(*args):
                calls.append(args)
                if args[0] == "lsblk":
                    return device_data
                if args[0] == "findmnt":
                    return "ABCD\n" if args[3] == "UUID" else "ext4\n"
                if args[0] == "umount":
                    # The mocked mount did not hide the fixture; leave cleanup to TemporaryDirectory.
                    return ""
                return ""

            with mock.patch.object(manager, "command", side_effect=fake_command), \
                 mock.patch.object(manager.tempfile, "mkdtemp", return_value=str(mountpoint)), \
                 mock.patch.object(Path, "rmdir"):
                result = manager.scan()
            self.assertEqual(len(result.configs), 1)
            self.assertEqual([c[0] for c in calls].count("mount"), 1)
            self.assertEqual([c[0] for c in calls].count("umount"), 1)
            self.assertIn("ro,noload,nosuid,nodev,noexec", calls[1])

    def test_existing_mount_is_not_unmounted_and_nested_boot_uses_its_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "boot/grub/grub.cfg"
            config.parent.mkdir(parents=True)
            config.write_text("menuentry 'Linux' {\n linux /vmlinuz quiet\n}\n", encoding="utf-8")
            data = json.dumps({"blockdevices": [{
                "path": "/dev/root", "fstype": "ext4", "uuid": "ROOT",
                "mountpoints": [tmp],
            }]})

            def fake_command(*args):
                if args[0] == "lsblk":
                    return data
                if args[0] == "findmnt":
                    return "BOOT\n" if args[3] == "UUID" else "vfat\n"
                self.fail(f"Unexpected command: {args}")

            with mock.patch.object(manager, "command", side_effect=fake_command):
                result = manager.scan()
            self.assertEqual(len(result.configs), 1)
            self.assertIn("--set=root BOOT", result.configs[0].content)
            self.assertIn("insmod fat", result.configs[0].content)

    def test_unmount_failure_is_reported_and_mountpoint_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            mountpoint = Path(tmp) / "mount"
            mountpoint.mkdir()
            data = json.dumps({"blockdevices": [{
                "path": "/dev/test", "fstype": "ext4", "uuid": "ABCD",
                "mountpoints": [],
            }]})

            def fake_command(*args):
                if args[0] == "lsblk":
                    return data
                if args[0] == "umount":
                    raise RuntimeError("busy")
                return ""

            with mock.patch.object(manager, "command", side_effect=fake_command), \
                 mock.patch.object(manager.tempfile, "mkdtemp", return_value=str(mountpoint)):
                result = manager.scan()
            self.assertTrue(mountpoint.exists())
            self.assertTrue(any("still mounted" in w and str(mountpoint) in w
                                for w in result.warnings))

    def test_existing_btrfs_is_scanned_even_if_top_level_mount_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "existing"
            config = root / "boot/grub/grub.cfg"
            config.parent.mkdir(parents=True)
            config.write_text("menuentry 'Linux' {\n linux /vmlinuz\n}\n", encoding="utf-8")
            data = json.dumps({"blockdevices": [{
                "path": "/dev/test", "fstype": "btrfs", "uuid": "ABCD",
                "mountpoints": [str(root)],
            }]})

            def fake_command(*args):
                if args[0] == "lsblk":
                    return data
                if args[0] == "mount":
                    raise RuntimeError("mount failed")
                if args[0] == "findmnt":
                    return "ABCD\n" if args[3] == "UUID" else "btrfs\n"
                self.fail(f"Unexpected command: {args}")

            with mock.patch.object(manager, "command", side_effect=fake_command):
                result = manager.scan()
            self.assertEqual(len(result.configs), 1)
            self.assertTrue(any("mount failed" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
