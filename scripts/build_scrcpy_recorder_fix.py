#!/usr/bin/env python3
"""Build and test the pinned scrcpy recorder fix without replacing system tools.

Requires curl, patch, Meson, Ninja, a C compiler and scrcpy's native development
dependencies (on macOS: the existing Homebrew FFmpeg, SDL3 and libusb).
Run with Artemis's Python and --activate to select it in this checkout's .env.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, UTC
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "5.0.1"
SOURCE_SHA256 = "a24b996ac23d0f674d3237c00b39a97829d8acbe9e1657a6c216fd51ea488ee5"
SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"
PATCH = ROOT / "patches/scrcpy/5.0.1-recorder-startup.patch"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, path: Path, expected: str) -> None:
    if not path.exists() or sha256(path) != expected:
        partial = path.with_suffix(path.suffix + ".partial")
        subprocess.run(["curl", "-fL", "--max-time", "120", url, "-o", str(partial)], check=True)
        if sha256(partial) != expected:
            raise RuntimeError(f"SHA-256 mismatch: {partial}")
        partial.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--prefix", type=Path, default=ROOT / ".cache/toolchains/scrcpy-recorder-fix"
    )
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    cache = ROOT / ".cache/scrcpy-recorder-fix"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"scrcpy-v{VERSION}.tar.gz"
    server = cache / f"scrcpy-server-v{VERSION}"
    download(
        f"https://codeload.github.com/Genymobile/scrcpy/tar.gz/refs/tags/v{VERSION}",
        archive,
        SOURCE_SHA256,
    )
    download(
        f"https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}",
        server,
        SERVER_SHA256,
    )

    # Each build starts from verified upstream inputs; no stale or partially
    # patched source tree can be silently reused.
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temp:
        work = Path(temp)
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        source = work / f"scrcpy-{VERSION}"
        subprocess.run(["patch", "--batch", "-p1", "-i", str(PATCH)], cwd=source, check=True)
        for kind in ("debug", "release"):
            build = work / kind
            subprocess.run(
                [
                    "meson",
                    "setup",
                    str(build),
                    str(source),
                    f"--buildtype={kind}",
                    f"--prefix={prefix}",
                    f"-Dprebuilt_server={server}",
                ],
                check=True,
            )
            subprocess.run(["meson", "compile", "-C", str(build)], check=True)
            if kind == "debug":
                subprocess.run(["meson", "test", "-C", str(build), "--print-errorlogs"], check=True)
                reports = prefix / "reports"
                reports.mkdir(parents=True, exist_ok=True)
                for name in ("testlog.txt", "testlog.json"):
                    shutil.copy2(build / "meson-logs" / name, reports / name)
            else:
                subprocess.run(["meson", "install", "-C", str(build), "--no-rebuild"], check=True)

    binary = prefix / "bin/scrcpy"
    receipt = {
        "built_at": datetime.now(UTC).isoformat(),
        "version": VERSION,
        "source_sha256": SOURCE_SHA256,
        "server_sha256": SERVER_SHA256,
        "patch_sha256": sha256(PATCH),
        "binary": str(binary),
        "binary_sha256": sha256(binary),
        "native_tests_passed": True,
    }
    (prefix / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if args.activate:
        from dotenv import set_key

        # Existing explicit tool selection is the only activation mechanism;
        # this does not alter Homebrew or other applications' scrcpy.
        set_key(ROOT / ".env", "ARTEMIS_SCRCPY_PATH", str(binary))
    print(json.dumps(receipt, indent=2))
    if args.activate:
        print("Selected in .env. New Artemis workers will use this binary.")


if __name__ == "__main__":
    main()
