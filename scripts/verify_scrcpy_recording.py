#!/usr/bin/env python3
"""Real-device recording acceptance; UI exploration/actions belong to Artemis MCP.

Exercises the shared scrcpy command and both production recording APIs. Keeps
every recording and log, rejects hidden watchdog restarts, checks audio/video
tracks, and decodes every output. No taps, settings changes or UiAutomation.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, UTC
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from artemis.config import settings
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.runtime import DeviceExecutionLock
from artemis.toolchain import find_scrcpy
from artemis.utils.video import build_scrcpy_record_command, get_ffprobe_path
from third_party.mobile_use.utils.video import get_active_session


def inspect_video(path: Path) -> dict:
    probe = subprocess.run(
        [
            get_ffprobe_path(),
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    metadata = json.loads(probe.stdout)
    streams = metadata["streams"]
    assert {s["codec_type"] for s in streams} >= {"audio", "video"}, metadata
    assert float(metadata["format"]["duration"]) > 0, metadata
    # ffprobe -count_frames decodes the input without adding an output muxer
    # whose time base can introduce unrelated DTS-quantization warnings.
    assert not probe.stderr.strip(), probe.stderr
    assert all(int(s.get("nb_read_frames", 0)) > 0 for s in streams), metadata
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "duration": metadata["format"]["duration"],
        "codecs": [s["codec_name"] for s in streams],
        "decoded_frames": {s["codec_type"]: int(s["nb_read_frames"]) for s in streams},
        "decoded": True,
    }


async def save_process_log(process, output: Path) -> None:
    parts = []
    for stream in (process.stdout, process.stderr):
        if stream:
            parts.append(await stream.read())
    output.write_bytes(b"\n".join(parts))


async def run(args: argparse.Namespace, report: dict) -> None:
    from adbutils import AdbClient

    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    binary = Path(find_scrcpy())
    report.update(
        {
            "started_at": datetime.now(UTC).isoformat(),
            "device": args.device_id,
            "binary": str(binary),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "runs": [],
            "passed": False,
        }
    )
    diagnostics = Path.home() / "Library/Logs/DiagnosticReports"
    before = set(diagnostics.glob("scrcpy*.ips"))
    lock = DeviceExecutionLock(args.device_id, description="scrcpy recording acceptance")
    await asyncio.to_thread(lock.acquire, timeout=10)
    controller = None
    try:
        for index in range(args.rounds):
            output = root / f"startup-{index:03d}.mkv"
            command = build_scrcpy_record_command(str(binary), args.device_id, output)
            command += ["--time-limit=2"]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=25)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            output.with_suffix(".log").write_bytes(stdout)
            assert process.returncode == 0, (process.returncode, stdout.decode(errors="replace"))
            report["runs"].append(
                {
                    "kind": "shared-command",
                    "exit_code": process.returncode,
                    **await asyncio.to_thread(inspect_video, output),
                }
            )
            print(f"startup {index + 1}/{args.rounds}: video+audio decoded", flush=True)

        client = AdbClient(host=settings.ADB_HOST or "127.0.0.1", port=settings.ADB_PORT or 5037)
        ctx = ArtemisContext(
            device=DeviceContext(
                host_platform=sys.platform,
                mobile_platform=DevicePlatform.ANDROID,
                device_id=args.device_id,
            ),
            adb_client=client,
        )
        controller = UnifiedMobileController(ctx)
        for index in range(args.api_rounds):
            output = root / f"controller-{index:03d}"
            started = await controller.start_video_recording(output_dir=output)
            assert started.success, started.message
            session = get_active_session(args.device_id)
            assert session and session.process
            process = session.process
            await asyncio.sleep(2)
            assert process.returncode is None, process.returncode
            assert session.android_segment_index == 0, "Watchdog hid a recorder restart"
            stopped = await controller.stop_video_recording()
            await save_process_log(process, output / "scrcpy.log")
            assert stopped.success and stopped.video_path, stopped.message
            assert process.returncode == 0, process.returncode
            report["runs"].append(
                {
                    "kind": "controller",
                    "exit_code": process.returncode,
                    **await asyncio.to_thread(inspect_video, stopped.video_path),
                }
            )
            print(f"controller {index + 1}/{args.api_rounds}: video+audio decoded", flush=True)

        for index in range(args.api_rounds):
            output = root / f"driver-{index:03d}"
            await controller.driver.start_video_recording(output_dir=output)
            process = controller.driver._scrcpy_process
            assert process and process.returncode is None
            await asyncio.sleep(2)
            assert process.returncode is None, process.returncode
            path = await controller.driver.stop_video_recording()
            await save_process_log(process, output / "scrcpy.log")
            assert path, "Driver did not finalize recording"
            assert process.returncode == 0, process.returncode
            report["runs"].append(
                {
                    "kind": "driver",
                    "exit_code": process.returncode,
                    **await asyncio.to_thread(inspect_video, Path(path)),
                }
            )
            print(f"driver {index + 1}/{args.api_rounds}: video+audio decoded", flush=True)
        report["new_crash_reports"] = [
            str(p) for p in set(diagnostics.glob("scrcpy*.ips")) - before
        ]
        assert not report["new_crash_reports"], report["new_crash_reports"]
        report["passed"] = True
    finally:
        if controller and get_active_session(args.device_id):
            await controller.stop_video_recording()
        if controller and getattr(controller.driver, "_scrcpy_process", None):
            await controller.driver.stop_video_recording()
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--api-rounds", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1 or args.api_rounds < 1:
        parser.error("Both round counts must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    try:
        asyncio.run(run(args, report))
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
