"""Read-only discovery of verifiable Windows UEFI boot managers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

from grub_manager import FoundConfig, MARKER, UUID, WINDOWS_MARKER, command
from i18n import _ as tr


BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
EFI_LOADER = ("EFI", "Microsoft", "Boot", "bootmgfw.efi")
BCD_PATH = ("EFI", "Microsoft", "Boot", "BCD")


@dataclass(frozen=True)
class Partition:
    disk: str
    part: str


@dataclass(frozen=True)
class WindowsLoader:
    device: Partition
    os_device: Partition
    loader_path: tuple[str, ...]
    system_root: tuple[str, ...]


def _guid(text: str) -> str:
    return str(uuid.UUID(text.strip("{}")))


def _windows_path(value: str) -> tuple[str, ...]:
    if not value.startswith("\\") or "/" in value:
        raise ValueError(tr("BCD path is not absolute or contains invalid characters."))
    parts = value[1:].split("\\")
    if any(not p or p in (".", "..") or ":" in p or "\x00" in p for p in parts):
        raise ValueError(tr("BCD path contains invalid path components."))
    return tuple(parts)


def _case_file(root: Path, parts: tuple[str, ...]) -> Path | None:
    current = root
    for part in parts:
        if not current.is_dir():
            return None
        with os.scandir(current) as entries:
            matches = [entry.name for entry in entries if entry.name.casefold() == part.casefold()]
        if len(matches) > 1:
            raise ValueError(tr("Ambiguous filename under {path}: {name}").format(path=current, name=part))
        if not matches:
            return None
        current = current / matches[0]
        if current.is_symlink():
            raise ValueError(tr("Symbolic link will not be followed: {path}").format(path=current))
    return current if current.is_file() else None


def _check_signature(path: Path, signature: bytes) -> None:
    with path.open("rb") as file:
        if file.read(len(signature)) != signature:
            raise ValueError(tr("Unexpected file format: {name}").format(name=path.name))


def _partition(data: bytes) -> Partition:
    # Registry device descriptors store the partition GUID at 0x20 and disk GUID at 0x38.
    if (len(data) != 88 or data[:16] != bytes(16)
            or int.from_bytes(data[16:20], "little") != 6
            or int.from_bytes(data[24:28], "little") != 72
            or data[48:56] != bytes(8)):
        raise ValueError(tr("Unsupported BCD device identifier (GPT partitions only)."))
    part = uuid.UUID(bytes_le=data[32:48])
    disk = uuid.UUID(bytes_le=data[56:72])
    if not part.int or not disk.int:
        raise ValueError(tr("Empty GPT identifier in BCD."))
    return Partition(str(disk), str(part))


def read_bcd(path: Path) -> list[WindowsLoader]:
    try:
        import hivex
    except ImportError as exc:
        raise RuntimeError(tr("Python HiveX is missing; Windows BCD cannot be read.")) from exc
    with path.open("rb") as file:
        header = file.read(12)
    if (len(header) != 12 or header[:4] != b"regf"
            or header[4:8] != header[8:12]):
        raise ValueError(tr("BCD hive is invalid or contains uncommitted transactions."))
    hive = hivex.Hivex(str(path))

    def child(node, name):
        result = hive.node_get_child(node, name)
        if result is None:
            raise ValueError(tr("BCD key is missing: {name}").format(name=name))
        return result

    def value(node, name, kind):
        handle = hive.node_get_value(node, name)
        if handle is None:
            raise ValueError(tr("BCD value is missing: {name}").format(name=name))
        actual, data = hive.value_value(handle)
        if actual != kind:
            raise ValueError(tr("Unexpected data type for BCD value {name}.").format(name=name))
        return handle, data

    def element(node, name, kind):
        return value(child(child(node, "Elements"), name), "Element", kind)

    objects = child(hive.root(), "Objects")
    manager = child(objects, BOOTMGR)
    order_handle, _ = element(manager, "24000001", 7)
    order = hive.value_multiple_strings(order_handle)
    if order and order[-1] == "":
        order.pop()
    default_handle, _ = element(manager, "23000003", 1)
    default = _guid(hive.value_string(default_handle))
    ids = list(dict.fromkeys([_guid(item) for item in order] + [default]))
    if not ids or not order:
        raise ValueError(tr("BCD contains no Windows boot menu entries."))
    result = []
    for item in ids:
        obj = child(objects, "{" + item + "}")
        _, raw_type = value(child(obj, "Description"), "Type", 4)
        if len(raw_type) != 4 or int.from_bytes(raw_type, "little") != 0x10200003:
            raise ValueError(tr("BCD entry {entry} is not a supported Windows UEFI loader.").format(entry=item))
        _, device = element(obj, "11000001", 3)
        _, os_device = element(obj, "21000001", 3)
        loader_handle, _ = element(obj, "12000002", 1)
        root_handle, _ = element(obj, "22000002", 1)
        loader_path = _windows_path(hive.value_string(loader_handle))
        if loader_path[-1].casefold() != "winload.efi":
            raise ValueError(tr("BCD entry {entry} does not use a Windows UEFI loader.").format(entry=item))
        result.append(WindowsLoader(
            _partition(device), _partition(os_device), loader_path,
            _windows_path(hive.value_string(root_handle)),
        ))
    return result


@contextmanager
def _mounted(device: dict, warnings: list[str]):
    mounts = [p for p in (device.get("mountpoints") or []) if p and p.startswith("/")]
    if mounts:
        yield Path(mounts[0])
        return
    mountpoint = Path(tempfile.mkdtemp(prefix="gos-grub-windows-"))
    mounted = False
    try:
        fstype = device["fstype"]
        mount_type = "ntfs-3g" if fstype in ("ntfs", "ntfs3") else fstype
        command("mount", "-t", mount_type, "-o", "ro,nosuid,nodev,noexec",
                "--", device["path"], str(mountpoint))
        mounted = True
        yield mountpoint
    finally:
        if mounted:
            try:
                command("umount", "--", str(mountpoint))
            except (OSError, RuntimeError) as exc:
                warnings.append(tr("WARNING: {path} is still mounted: {error}").format(path=mountpoint, error=exc))
            else:
                mountpoint.rmdir()
        else:
            mountpoint.rmdir()


def scan_windows(devices: list[dict]) -> tuple[list[FoundConfig], list[str]]:
    configs = []
    warnings = []
    partitions: dict[Partition, list[dict]] = {}
    filesystem_uuids: dict[str, int] = {}
    for device in devices:
        fs_uuid = device.get("uuid")
        if fs_uuid:
            fs_uuid = fs_uuid.lower()
            filesystem_uuids[fs_uuid] = filesystem_uuids.get(fs_uuid, 0) + 1
        try:
            key = Partition(_guid(device["ptuuid"]), _guid(device["partuuid"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        partitions.setdefault(key, []).append(device)

    for esp in devices:
        if esp.get("fstype") != "vfat":
            continue
        try:
            with _mounted(esp, warnings) as root:
                loader = _case_file(root, EFI_LOADER)
                if loader is None:
                    continue
                _check_signature(loader, b"MZ")
                bcd = _case_file(root, BCD_PATH)
                if bcd is None:
                    raise ValueError(tr("Windows bootloader found, but BCD is missing."))
                try:
                    esp_partition = Partition(_guid(esp["ptuuid"]), _guid(esp["partuuid"]))
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    raise ValueError(tr("EFI partition is not a uniquely identifiable GPT partition.")) from exc
                if len(partitions.get(esp_partition, [])) != 1:
                    raise ValueError(tr("EFI GPT partition is not unique."))
                fs_uuid = esp.get("uuid")
                if (not fs_uuid or not UUID.fullmatch(fs_uuid)
                        or filesystem_uuids.get(fs_uuid.lower()) != 1):
                    raise ValueError(tr("EFI filesystem UUID is missing or not unique."))
                entries = read_bcd(bcd)
                loader_path = "/" + loader.relative_to(root).as_posix()
            for entry in entries:
                needed = {}
                for partition, parts, signature in (
                    (entry.device, entry.loader_path, b"MZ"),
                    (entry.os_device, entry.system_root + ("System32", "config", "SYSTEM"), b"regf"),
                    (entry.os_device, entry.system_root + ("System32", "ntoskrnl.exe"), b"MZ"),
                ):
                    needed.setdefault(partition, []).append((parts, signature))
                for partition, paths in needed.items():
                    candidates = partitions.get(partition, [])
                    if len(candidates) != 1 or candidates[0].get("fstype") not in ("ntfs", "ntfs3"):
                        raise ValueError(tr("BCD partition is missing, not unique, or not readable NTFS."))
                    with _mounted(candidates[0], warnings) as root:
                        for parts, signature in paths:
                            path = _case_file(root, parts)
                            if path is None:
                                raise ValueError(tr("Windows file is missing on {device}: {path}").format(
                                    device=candidates[0]["path"], path="\\".join(parts)))
                            _check_signature(path, signature)
            content = (
                MARKER + WINDOWS_MARKER + 'if [ "$grub_platform" = efi ]; then\n'
                "menuentry 'Windows Boot Manager (UEFI)' --class windows --class os {\n"
                "    insmod part_gpt\n    insmod fat\n"
                f"    if search --no-floppy --fs-uuid --set=root {fs_uuid}; then\n"
                f"        chainloader {loader_path}\n"
                "    else\n        echo 'Windows EFI partition not found.'\n"
                "    fi\n}\nfi\n"
            )
            key = hashlib.sha256(fs_uuid.lower().encode()).hexdigest()[:10]
            configs.append(FoundConfig(esp["path"], "/".join(EFI_LOADER),
                                       "Windows Boot Manager (UEFI)", f"windows-{key}.cfg",
                                       content, 1))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            warnings.append(tr("{path}: Windows UEFI not imported: {error}").format(
                path=esp["path"], error=exc))
    return configs, warnings
