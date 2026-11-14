import gettext
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import grub_manager
import windows_uefi as windows


def setUpModule():
    patcher = mock.patch.object(windows, "tr", gettext.NullTranslations().gettext)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


DISK = uuid.UUID("1407e695-4086-4f2c-9e93-f26b44792c44")
PART = uuid.UUID("9ce00fe0-2a66-4db4-b11e-dee439cba252")
ENTRY = "{abcdef12-0000-4000-8000-0123456789ab}"


def descriptor(disk=DISK, part=PART):
    data = bytearray(88)
    data[16:20] = (6).to_bytes(4, "little")
    data[24:28] = (72).to_bytes(4, "little")
    data[32:48] = part.bytes_le
    data[56:72] = disk.bytes_le
    return bytes(data)


def registry_element(kind, data):
    return {"Element": (kind, data)}


def hive_tree():
    return {"Objects": {
        windows.BOOTMGR: {"Elements": {
            "24000001": registry_element(7, [ENTRY, ""]),
            "23000003": registry_element(1, ENTRY),
        }},
        ENTRY: {
            "Description": {"Type": (4, (0x10200003).to_bytes(4, "little"))},
            "Elements": {
                "11000001": registry_element(3, descriptor()),
                "21000001": registry_element(3, descriptor()),
                "12000002": registry_element(1, "\\Windows\\system32\\winload.efi"),
                "22000002": registry_element(1, "\\Windows"),
            },
        },
    }}


class FakeHive:
    tree = None

    def __init__(self, _path):
        self.data = self.tree

    def root(self):
        return self.data

    def node_get_child(self, node, key):
        return next((value for name, value in node.items()
                     if name.casefold() == key.casefold()), None)

    def node_get_value(self, node, key):
        return self.node_get_child(node, key)

    def value_value(self, value):
        return value

    def value_string(self, value):
        return value[1]

    def value_multiple_strings(self, value):
        return list(value[1])


class BcdTests(unittest.TestCase):
    def read(self, tree, header=b"regf" + b"\x01\0\0\0" * 2):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BCD"
            path.write_bytes(header)
            FakeHive.tree = tree
            with mock.patch.dict("sys.modules", {"hivex": SimpleNamespace(Hivex=FakeHive)}):
                return windows.read_bcd(path)

    def test_reads_gpt_devices_and_bcd_paths(self):
        result = self.read(hive_tree())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].device, windows.Partition(str(DISK), str(PART)))
        self.assertEqual(result[0].os_device, result[0].device)
        self.assertEqual(result[0].loader_path, ("Windows", "system32", "winload.efi"))
        self.assertEqual(result[0].system_root, ("Windows",))

    def test_refuses_unverified_display_or_default_entry(self):
        tree = hive_tree()
        tree["Objects"][windows.BOOTMGR]["Elements"]["24000001"] = registry_element(
            7, [ENTRY, "{01234567-0000-4000-8000-0123456789ab}", ""])
        with self.assertRaisesRegex(ValueError, "BCD key is missing"):
            self.read(tree)

    def test_rejects_dirty_hive_and_unsupported_device(self):
        with self.assertRaisesRegex(ValueError, "transactions"):
            self.read(hive_tree(), b"regf" + b"\x01\0\0\0" + b"\x02\0\0\0")
        for data in (descriptor()[:40], bytes(88), descriptor()[:48] + b"\x01" + descriptor()[49:]):
            tree = hive_tree()
            tree["Objects"][ENTRY]["Elements"]["21000001"] = registry_element(3, data)
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "device identifier"):
                self.read(tree)

    def test_refuses_bios_or_unsafe_paths(self):
        for path in ("\\Windows\\system32\\winload.exe", "\\..\\winload.efi",
                     "C:\\Windows\\winload.efi"):
            tree = hive_tree()
            tree["Objects"][ENTRY]["Elements"]["12000002"] = registry_element(1, path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.read(tree)


class WindowsScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.esp = self.root / "esp"
        self.ntfs = self.root / "ntfs"
        self.write(self.esp / "EFI/Microsoft/Boot/bootmgfw.efi", b"MZtest")
        self.write(self.esp / "EFI/Microsoft/Boot/BCD", b"BCD")
        self.write(self.ntfs / "WINDOWS/System32/winload.efi", b"MZtest")
        self.write(self.ntfs / "WINDOWS/System32/ntoskrnl.exe", b"MZtest")
        self.write(self.ntfs / "WINDOWS/System32/config/SYSTEM", b"regftest")
        self.devices = [
            {"path": "/dev/esp", "fstype": "vfat", "uuid": "A1B2-C3D4",
             "partuuid": "1ce00fe0-2a66-4db4-b11e-dee439cba252",
             "ptuuid": str(DISK), "mountpoints": [str(self.esp)]},
            {"path": "/dev/windows", "fstype": "ntfs", "uuid": "D123",
             "partuuid": str(PART), "ptuuid": str(DISK), "mountpoints": [str(self.ntfs)]},
        ]
        self.entry = windows.WindowsLoader(windows.Partition(str(DISK), str(PART)),
                                           windows.Partition(str(DISK), str(PART)),
                                           ("Windows", "system32", "winload.efi"), ("Windows",))

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def scan(self, entries=None):
        with mock.patch.object(windows, "read_bcd", return_value=entries or [self.entry]):
            return windows.scan_windows(self.devices)

    def test_detects_confirmed_windows_and_grub_cfg_is_uefi_only(self):
        configs, warnings = self.scan()
        self.assertEqual(len(configs), 1)
        self.assertEqual(warnings, [])
        self.assertTrue(configs[0].content.startswith(grub_manager.MARKER +
                                                     grub_manager.WINDOWS_MARKER))
        self.assertIn('if [ "$grub_platform" = efi ]; then', configs[0].content)
        self.assertIn("chainloader /EFI/Microsoft/Boot/bootmgfw.efi", configs[0].content)
        self.assertIn("--set=root A1B2-C3D4", configs[0].content)
        grub_manager._validate(configs[0].content)

    def test_rejects_missing_windows_files_and_ambiguous_devices(self):
        (self.ntfs / "WINDOWS/System32/config/SYSTEM").unlink()
        configs, warnings = self.scan()
        self.assertEqual(configs, [])
        self.assertIn("Windows file is missing", warnings[0])
        self.write(self.ntfs / "WINDOWS/System32/config/SYSTEM", b"regftest")
        self.devices.append(dict(self.devices[1], path="/dev/duplicate"))
        configs, warnings = self.scan()
        self.assertEqual(configs, [])
        self.assertTrue(any("not unique" in warning for warning in warnings))

    def test_one_stale_bcd_entry_prevents_boot_manager_from_being_added(self):
        stale = windows.WindowsLoader(
            windows.Partition(str(DISK), str(uuid.uuid4())), self.entry.os_device,
            self.entry.loader_path, self.entry.system_root,
        )
        configs, warnings = self.scan([self.entry, stale])
        self.assertEqual(configs, [])
        self.assertIn("BCD partition is missing", warnings[0])

    def test_requires_an_identifiable_gpt_efi_partition(self):
        self.devices[0]["ptuuid"] = None
        configs, warnings = self.scan()
        self.assertEqual(configs, [])
        self.assertIn("GPT partition", warnings[0])

    def test_efi_and_windows_signatures_must_be_valid(self):
        (self.esp / "EFI/Microsoft/Boot/bootmgfw.efi").write_bytes(b"not efi")
        configs, warnings = self.scan()
        self.assertEqual(configs, [])
        self.assertIn("file format", warnings[0])

    def test_missing_and_invalid_bcd_are_reported(self):
        bcd = self.esp / "EFI/Microsoft/Boot/BCD"
        bcd.unlink()
        configs, warnings = self.scan()
        self.assertEqual(configs, [])
        self.assertIn("BCD is missing", warnings[0])
        self.write(bcd, b"bad")
        configs, warnings = windows.scan_windows(self.devices)
        self.assertEqual(configs, [])
        self.assertIn("BCD hive is invalid", warnings[0])

    def test_existing_mounts_are_not_unmounted(self):
        with mock.patch.object(windows, "command") as command:
            with windows._mounted(self.devices[1], []) as mount:
                self.assertEqual(mount, self.ntfs)
        command.assert_not_called()

    def test_mount_failure_removes_unused_temporary_directory(self):
        mountpoint = self.root / "mount"
        mountpoint.mkdir()
        device = dict(self.devices[1], mountpoints=[])
        with mock.patch.object(windows, "command", side_effect=RuntimeError("mount failed")), \
             mock.patch.object(windows.tempfile, "mkdtemp", return_value=str(mountpoint)):
            with self.assertRaisesRegex(RuntimeError, "mount failed"):
                with windows._mounted(device, []):
                    self.fail("mount failed")
        self.assertFalse(mountpoint.exists())

    def test_windows_path_symlink_is_not_followed(self):
        os.symlink(self.root, self.ntfs / "link")
        with self.assertRaisesRegex(ValueError, "Symbolic link"):
            windows._case_file(self.ntfs, ("link", "secret"))

    def test_temporary_ntfs_mount_is_read_only_and_always_unmounted(self):
        mountpoint = self.root / "mount"
        mountpoint.mkdir()
        device = dict(self.devices[1], mountpoints=[])
        calls = []

        def fake_command(*args):
            calls.append(args)
            return ""

        with mock.patch.object(windows, "command", side_effect=fake_command), \
             mock.patch.object(windows.tempfile, "mkdtemp", return_value=str(mountpoint)):
            with self.assertRaisesRegex(ValueError, "failure"):
                with windows._mounted(device, []):
                    raise ValueError("failure")
        self.assertEqual(calls[0][:5], ("mount", "-t", "ntfs-3g", "-o",
                                        "ro,nosuid,nodev,noexec"))
        self.assertEqual(calls[1][0], "umount")
        self.assertFalse(mountpoint.exists())

    def test_unmount_failure_does_not_hide_verification_error(self):
        mountpoint = self.root / "mount"
        mountpoint.mkdir()
        device = dict(self.devices[1], mountpoints=[])
        warnings = []

        def fake_command(*args):
            if args[0] == "umount":
                raise RuntimeError("busy")
            return ""

        with mock.patch.object(windows, "command", side_effect=fake_command), \
             mock.patch.object(windows.tempfile, "mkdtemp", return_value=str(mountpoint)):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                with windows._mounted(device, warnings):
                    raise ValueError("verification failed")
        self.assertTrue(mountpoint.exists())
        self.assertIn("still mounted", warnings[0])


if __name__ == "__main__":
    unittest.main()
