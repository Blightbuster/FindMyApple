"""Fetch Apple's macOS CoreADI library from Apple, for the emulated-Mac anisette provider.

No Home Assistant imports: the spike scripts and the tests use this module directly.

The library is never shipped with this integration. It sits inside the iTunes 12.8.3 for Mac
installer, which Apple still serves. In that disk image the package payload holding it is stored
gzip-compressed and contiguous, so one HTTP range request of ~3.5 MB fetches it. Two SHA-256 pins
check what came back: the payload, and the x86_64 slice extracted from it. `machoadi` only knows
the dispatcher offset of exactly this build, so any other bytes are refused.

Adapted from OpenTagViewer (MIT, see THIRD_PARTY_NOTICES.md); constants as published there and in os-findmy-e.
"""

from __future__ import annotations

import gzip
import hashlib
import struct
import urllib.request
from pathlib import Path

DMG_URL = (
    "https://secure-appldnld.apple.com/itunes12/"
    "041-56365-20210413-79A5AC6D-6B0A-46EC-B399-5A381183E3BE/iTunes12.8.3.dmg"
)
PAYLOAD_OFFSET = 184_678_769
PAYLOAD_LENGTH = 3_471_595
PAYLOAD_SHA256 = "8a6c068c95036326eb2d2885fa60912ed0f7229d0939ae92934a70a3bb6cf025"
LIBRARY_SHA256 = "2eafc3c79c7f41a8afbfbeaaf23ee5dab5ad5bda55ec8e54dbc0ddf1d20ce630"
FRAMEWORK_PATH = "System/Library/PrivateFrameworks/CoreADI.framework/Versions/A/CoreADI"
LIBRARY_FILENAME = "CoreADI.x86_64"
_CPU_TYPE_X86_64 = 0x01000007


class CoreAdiError(RuntimeError):
    """Apple's library could not be downloaded or is not the expected build."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_valid_library(path: Path) -> bool:
    return path.is_file() and sha256(path.read_bytes()) == LIBRARY_SHA256


def ensure_library(target: Path, url: str = DMG_URL, timeout: float = 60) -> Path:
    """Make sure `target` holds Apple's x86_64 CoreADI 4.19, downloading it if needed. Blocking."""
    if is_valid_library(target):
        return target
    library = extract(fetch_payload(url, timeout))
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(library)
    partial.replace(target)  # atomic: an interrupted write never leaves a truncated library
    return target


def fetch_payload(url: str = DMG_URL, timeout: float = 60) -> bytes:
    end = PAYLOAD_OFFSET + PAYLOAD_LENGTH - 1
    request = urllib.request.Request(url, headers={"Range": f"bytes={PAYLOAD_OFFSET}-{end}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Anything but 206 means the range was ignored; reading on would pull 291 MB.
            if response.status != 206:
                raise CoreAdiError(f"Apple answered HTTP {response.status} to the range request; expected 206")
            payload = response.read(PAYLOAD_LENGTH + 1)
    except OSError as err:
        raise CoreAdiError(f"Could not download Apple's CoreADI: {err}") from err
    if len(payload) != PAYLOAD_LENGTH:
        raise CoreAdiError(f"Expected {PAYLOAD_LENGTH} bytes of the installer, got {len(payload)}")
    return payload


def extract(payload: bytes) -> bytes:
    """The x86_64 library out of the installer payload. Bytes in, bytes out, no network."""
    if sha256(payload) != PAYLOAD_SHA256:
        raise CoreAdiError(f"The installer bytes are not the expected ones (sha256 {sha256(payload)})")
    library = _slice(_file_from_cpio(gzip.decompress(payload), FRAMEWORK_PATH), _CPU_TYPE_X86_64)
    if sha256(library) != LIBRARY_SHA256:
        raise CoreAdiError(f"The library is not the expected build (sha256 {sha256(library)})")
    return library


def _file_from_cpio(cpio: bytes, wanted: str) -> bytes:
    """One file from an "odc" cpio archive (76-byte ASCII-octal headers), as Apple pkgs use."""
    pos = 0
    while pos + 76 <= len(cpio):
        if cpio[pos : pos + 6] != b"070707":
            raise CoreAdiError(f"Not an odc cpio archive at offset {pos}")
        name_size = int(cpio[pos + 59 : pos + 65], 8)
        file_size = int(cpio[pos + 65 : pos + 76], 8)
        data = pos + 76 + name_size
        name = cpio[pos + 76 : data - 1].decode("utf-8").removeprefix("./")
        if name == "TRAILER!!!":
            break
        if name == wanted and file_size:  # hard-link twins carry no data
            return cpio[data : data + file_size]
        pos = data + file_size
    raise CoreAdiError(f"{wanted} is not in the installer payload")


def _slice(fat: bytes, cpu_type: int) -> bytes:
    """One architecture's slice out of a Mach-O universal binary."""
    if fat[:4] != b"\xca\xfe\xba\xbe":
        raise CoreAdiError("The library is not a universal binary")
    (count,) = struct.unpack(">I", fat[4:8])
    for i in range(count):
        cpu, _, offset, size, _ = struct.unpack(">5I", fat[8 + 20 * i : 28 + 20 * i])
        if cpu == cpu_type:
            return fat[offset : offset + size]
    raise CoreAdiError("The library has no x86_64 slice")


if __name__ == "__main__":
    import sys

    dest = Path(sys.argv[1] if len(sys.argv) > 1 else LIBRARY_FILENAME)
    print(ensure_library(dest))
