"""Build a Rufus-compatible FAT32 SD-card image for thermostat CA import."""

import json
from pathlib import Path
import re
import struct
import subprocess
import time


_VERSION = re.compile(r"^VH([0-9]{1,3})\.([0-9]{2})$")
_PARTITION_START = 2048  # 1 MiB alignment
_VOLUME_KIB = 64 * 1024
_SECTOR_SIZE = 512


def parse_firmware_version(value):
    """Return a display version and the four-byte Venstar firmware stub."""
    if not isinstance(value, str):
        raise ValueError("Enter the firmware version shown on the thermostat, such as VH6.93")
    match = _VERSION.fullmatch(value.strip().upper())
    if not match:
        raise ValueError("Firmware version must look like VH6.93, as shown on the About screen")
    major, minor = (int(part) for part in match.groups())
    if major > 255:
        raise ValueError("Firmware major version must be between 0 and 255")
    display = f"VH{major}.{minor:02d}"
    return display, bytes((ord("V"), ord("H"), minor, major))


def _run(command):
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required image utility is missing: {exc.filename}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SD-card image utility timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or "SD-card image utility failed") from exc


def build_image(output_path, firmware_version, root_ca_pem):
    """Create an MBR-partitioned raw image containing the importer files."""
    version, firmware_stub = parse_firmware_version(firmware_version)
    output_path = Path(output_path)
    volume_path = output_path.with_suffix(".fat32")
    volume_sectors = _VOLUME_KIB * 2

    try:
        _run(["mkfs.fat", "-F", "32", "-n", "COLORTOUCH", "-C",
              str(volume_path), str(_VOLUME_KIB)])
        for directory in ("VH", "VH/firmware1a", "VH/gallery"):
            _run(["mmd", "-i", str(volume_path), f"::/{directory}"])

        stub_path = output_path.with_suffix(".update.bin")
        gallery_path = output_path.with_suffix(".gallery.bin")
        stub_path.write_bytes(firmware_stub)
        gallery = {
            "time": int(time.time()), "origin": 1, "model": "VH",
            "version": version[2:],
            "imgs": [{"path": "../skyport_root.pem", "ssExclude": True,
                      "useAsBackground": False}],
        }
        gallery_path.write_text(json.dumps(gallery, separators=(",", ":")), encoding="utf-8")
        root_path = output_path.with_suffix(".root-ca.pem")
        root_path.write_bytes(root_ca_pem)

        for source, destination in (
            (stub_path, "::/VH/firmware1a/update.bin"),
            (gallery_path, "::/VH/gallery/gallery.bin"),
            (root_path, "::/VH/skyport_root.pem"),
        ):
            _run(["mcopy", "-i", str(volume_path), str(source), destination])

        # Wrap the FAT32 volume in a conventional MBR partition table so desktop
        # disk writers can flash the .img as a complete disk image.
        start = _PARTITION_START
        disk_sectors = start + volume_sectors + 2048
        with output_path.open("wb") as image:
            image.truncate(disk_sectors * _SECTOR_SIZE)
            mbr = bytearray(_SECTOR_SIZE)
            entry = bytearray(16)
            entry[0] = 0x00
            entry[1:4] = bytes((0x00, 0x02, 0x00))
            entry[4] = 0x0C  # FAT32 LBA
            entry[5:8] = bytes((0xFE, 0xFF, 0xFF))
            struct.pack_into("<II", entry, 8, start, volume_sectors)
            mbr[446:462] = entry
            mbr[510:512] = b"\x55\xaa"
            image.seek(0)
            image.write(mbr)
            image.seek(start * _SECTOR_SIZE)
            with volume_path.open("rb") as volume:
                while chunk := volume.read(1024 * 1024):
                    image.write(chunk)
        return output_path
    finally:
        for path in (volume_path, output_path.with_suffix(".update.bin"),
                     output_path.with_suffix(".gallery.bin"), output_path.with_suffix(".root-ca.pem")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
