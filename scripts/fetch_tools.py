"""Download the pinned yt-dlp and Deno binaries into a directory, for the
Dockerfile's tools stage (the slim image has no curl, so this is stdlib).

yt-dlp is the standalone Linux build, not the pip package: current releases
need Python 3.11+ and the bot runs 3.10. Deno is the JavaScript runtime
yt-dlp uses for YouTube's player challenges. Both are pinned by version and
sha256 per architecture; bump the version and the sums together.

Usage: python scripts/fetch_tools.py <dest dir> [<arch>]  (arch defaults to
the machine's: x86_64 or aarch64)
"""
import hashlib
import io
import os
import platform
import stat
import sys
import urllib.request
import zipfile

YTDLP_VERSION = "2026.10.26"
DENO_VERSION = "2.9.8"

# arch -> (yt-dlp asset, sha256, deno asset, sha256)
PINS = {
    "x86_64": (
        "yt-dlp_linux", "767ba9dce7052aa196f8b12652f433d76de128ba168966fa84b6394d5e6c6795",
        "deno-x86_64-unknown-linux-gnu.zip", "e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609",
    ),
    "aarch64": (
        "yt-dlp_linux_aarch64", "e87e9533e89fe5889abaa21f5114dfe7756d6aa6219b83551add04d48b18e483",
        "deno-aarch64-unknown-linux-gnu.zip", "d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905",
    ),
}
ARCH_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}  # Docker's TARGETARCH spellings


def fetch(url: str, sha256: str) -> bytes:
    print("fetching", url, flush=True)
    with urllib.request.urlopen(url, timeout=120) as r:  # nosec B310 - https URLs from the pins above
        data = r.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        sys.exit(f"checksum mismatch for {url}: got {digest}, expected {sha256}")
    return data


def main(dest: str, arch: str) -> None:
    arch = ARCH_ALIASES.get(arch, arch)
    if arch not in PINS:
        sys.exit(f"no pins for architecture {arch!r} (have {sorted(PINS)})")
    ytdlp_asset, ytdlp_sum, deno_asset, deno_sum = PINS[arch]
    os.makedirs(dest, exist_ok=True)
    executable = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH

    ytdlp = fetch(f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/{ytdlp_asset}", ytdlp_sum)
    path = os.path.join(dest, "yt-dlp")
    with open(path, "wb") as f:
        f.write(ytdlp)
    os.chmod(path, executable)

    deno_zip = fetch(f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/{deno_asset}", deno_sum)
    with zipfile.ZipFile(io.BytesIO(deno_zip)) as z:
        data = z.read("deno")
    path = os.path.join(dest, "deno")
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, executable)
    print("ok", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else platform.machine())
