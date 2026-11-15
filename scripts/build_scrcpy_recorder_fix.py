#!/usr/bin/env python3
"""Build and test the pinned scrcpy recorder fix without replacing system tools.

Requires curl, patch, Meson, Ninja, a C compiler and scrcpy's native development
dependencies (on macOS: the existing Homebrew FFmpeg, SDL3 and libusb).
Run with Artemis's Python and --activate to select it in this checkout's .env.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
import platform
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


def build_inputs() -> dict:
    return {
        "version": VERSION,
        "source_sha256": SOURCE_SHA256,
        "server_sha256": SERVER_SHA256,
        "patch_sha256": sha256(PATCH),
        "builder_sha256": sha256(Path(__file__)),
        "platform": platform.system(),
        "machine": platform.machine(),
    }


def current_build(prefix: Path) -> dict | None:
    """Require both matching inputs and intact installed client/server bytes."""
    try:
        receipt = json.loads((prefix / "build-receipt.json").read_text())
        binary = prefix / "bin/scrcpy"
        server = prefix / "share/scrcpy/scrcpy-server"
        if (
            all(receipt.get(key) == value for key, value in build_inputs().items())
            and receipt.get("native_tests_passed") is True
            and receipt.get("binary") == str(binary)
            and os.access(binary, os.X_OK)
            and receipt.get("binary_sha256") == sha256(binary)
            and sha256(server) == SERVER_SHA256
        ):
            return receipt
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def activate(prefix: Path) -> None:
    from dotenv import dotenv_values, set_key

    env_file = ROOT / ".env"
    # Do not rewrite unchanged config: its bytes participate in validation.
    values = dotenv_values(env_file)
    for key, value in {
        "ARTEMIS_SCRCPY_PATH": str(prefix / "bin/scrcpy"),
        "SCRCPY_SERVER_PATH": str(prefix / "share/scrcpy/scrcpy-server"),
    }.items():
        if values.get(key) != value:
            set_key(env_file, key, value)


def publish_installation(staged: Path, prefix: Path) -> None:
    """Replace complete files by rename, preserving running executable inodes."""
    if (
        not (staged / "bin/scrcpy").is_file()
        or not (staged / "share/scrcpy/scrcpy-server").is_file()
    ):
        raise RuntimeError("Staged scrcpy installation is incomplete")
    for source in sorted(staged.rglob("*")):
        if not source.is_file():
            continue
        destination = prefix / source.relative_to(staged)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copy2(source, temporary_path)
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)


def build(prefix: Path) -> dict:
    expected_inputs = build_inputs()
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
                staging = work / "install"
                subprocess.run(
                    [
                        "meson",
                        "install",
                        "-C",
                        str(build),
                        "--no-rebuild",
                        f"--destdir={staging}",
                    ],
                    check=True,
                )
                if build_inputs() != expected_inputs:
                    raise RuntimeError(
                        "scrcpy build inputs changed; installation was not published"
                    )
                publish_installation(staging / prefix.relative_to(prefix.anchor), prefix)

    binary = prefix / "bin/scrcpy"
    if build_inputs() != expected_inputs:
        raise RuntimeError("scrcpy build inputs changed during build; no success receipt written")
    receipt = {
        "built_at": datetime.now(UTC).isoformat(),
        **expected_inputs,
        "binary": str(binary),
        "binary_sha256": sha256(binary),
        "native_tests_passed": True,
    }
    temporary_receipt = prefix / "build-receipt.tmp"
    temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary_receipt.replace(prefix / "build-receipt.json")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--ensure", action="store_true", help="Reuse an intact build with matching inputs"
    )
    parser.add_argument(
        "--prefix", type=Path, default=ROOT / ".cache/toolchains/scrcpy-recorder-fix"
    )
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    receipt = current_build(prefix) if args.ensure else None
    if receipt is None:
        print(
            "Building patched scrcpy (inputs changed or verified installation unavailable).",
            flush=True,
        )
        receipt = build(prefix)
    else:
        print(
            "Patched scrcpy inputs and installed files unchanged; reusing verified build.",
            flush=True,
        )
    if args.activate:
        activate(prefix)
    print(json.dumps(receipt, indent=2))
    if args.activate:
        print("Selected in .env. New Artemis workers will use this binary.")


if __name__ == "__main__":
    main()
