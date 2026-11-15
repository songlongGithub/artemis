# scrcpy 5.0.1 recorder startup fix

The macOS crash reports show `avformat_new_stream(NULL, NULL)` in either packet
sink while the low-priority recorder worker is still finding the muxer or
allocating its context. Upstream 5.0.1 creates this context inside the worker;
starting a thread does not guarantee it runs before the demuxers.

`5.0.1-recorder-startup.patch` initializes the output synchronously in
`sc_recorder_start()` before any packet producer starts. It closes the output
if thread creation fails. Shutdown marks the recorder stopped and closes the
context under the sink mutex; both sinks reject late opens after shutdown.
Audio and video remain enabled.

The native regression test substitutes a thread launcher that holds the worker
without running it. It then opens both real packet sinks immediately. Unpatched
5.0.1 fails at `assert(recorder.ctx)`; the patch passes this deterministic
scheduling case, late opens after stop, thread-creation failure, and output-open
failure. The build also runs all upstream native tests with assertions enabled.

Build and select the fixed client from the repository root:

```sh
.venv/bin/python scripts/build_scrcpy_recorder_fix.py --activate
```

The script verifies SHA-256 of the official source archive and server, applies
the patch to a fresh source tree, runs the debug tests, then builds and installs
the release client under `.cache/toolchains/scrcpy-recorder-fix`. It records the
patch and binary hashes in `build-receipt.json` there. It needs Meson, Ninja,
curl, patch, a C compiler, and FFmpeg/SDL3/libusb development dependencies; it
does not install those dependencies. The server is the unchanged official
5.0.1 artifact, so no Android SDK build or helper APK change is needed.

`--activate` writes `ARTEMIS_SCRCPY_PATH` in the local `.env`. Both the controller
(including segment restarts) and Android driver resolve this explicit path.
New task workers load it; already running tasks keep their current executable.
Homebrew's system scrcpy is unchanged. To revert selection, remove that one
variable from `.env` and start a new task worker.

After exploring the target device through Artemis MCP, with no mobile task or
instrumented test using the device, run the recording-only acceptance:

```sh
.venv/bin/python scripts/verify_scrcpy_recording.py \
  --device-id SERIAL \
  --output-dir local-verification/scrcpy-recorder-fix
```

This holds the Artemis device lock and performs no UI actions. The default
test runs 30 shared-command starts, three controller sessions and three driver
sessions. It requires zero process failures, no controller watchdog restarts,
nonempty audio and video tracks, successful full frame decoding by ffprobe,
and no new macOS scrcpy crash reports. Logs, videos and a JSON report are kept.
Run a separate Artemis MCP UI task and inspect its final recording as the
end-to-end integration check. Repeated passing starts establish the tested
configuration's result; they do not prove every device/platform is crash-free.

Upstream: <https://github.com/Genymobile/scrcpy/tree/v5.0.1> (Apache-2.0).
