"""Install the checksum-pinned official Linux Godot used by the release verifier."""
from pathlib import Path
import argparse
import hashlib
import io
import os
import platform
import urllib.request
import zipfile

VERSION = "4.7.2"
ARCHIVE = f"Godot_v{VERSION}-stable_linux.x86_64.zip"
BINARY = f"Godot_v{VERSION}-stable_linux.x86_64"
URL = f"https://github.com/godotengine/godot-builds/releases/download/{VERSION}-stable/{ARCHIVE}"
# Official release SHA512-SUMS.txt, also saved with the verified client toolchain.
SHA512 = "9aa00f7a605200940bce3027a567b782f49bd8e940dd06ae9e987bd65aee1b1467edd56ed84fcdcbdd44354bf613bdbb4e5d2913e925850368e150c59ed54c65"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parent / ".runtime")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        parser.error("The pinned deploy verifier is for Linux x86_64; point PB_GODOT_BINARY at the verified matching local engine on other platforms")
    request = urllib.request.Request(URL, headers={"User-Agent": "Pong-Breaker-build/1.4"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(200 * 1024 * 1024 + 1)
    if len(data) > 200 * 1024 * 1024 or hashlib.sha512(data).hexdigest() != SHA512:
        raise RuntimeError("Official Godot archive checksum did not match; installation stopped")
    args.destination.mkdir(parents=True, exist_ok=True)
    target = args.destination / "godot"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        member = archive.getinfo(BINARY)
        if member.file_size > 300 * 1024 * 1024:
            raise RuntimeError("Unexpected Godot binary size")
        temporary = args.destination / "godot.pending"
        temporary.write_bytes(archive.read(member))
        temporary.chmod(0o755)
        temporary.replace(target)
    print(f"Verified Godot {VERSION} installed at {target}")


if __name__ == "__main__":
    main()
