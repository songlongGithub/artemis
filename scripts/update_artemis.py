#!/usr/bin/env python3
"""Fast-forward the tracked branch, reconcile tools, and cache successful checks."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UPDATER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
TESTS = [
    "tests/unit/mcp/test_diagnose_tool.py",
    "tests/unit/config/test_optional_llm_roles.py",
    "tests/unit/agents/test_local_model_routing.py",
    "tests/unit/test_personal_fork_scripts.py",
    "tests/unit/test_scrcpy_build.py",
    "tests/unit/runtime/test_trace_retention.py",
]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, **value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": 1, **value}, indent=2) + "\n")
    temporary.replace(path)


def file_identity(path: Path) -> dict | None:
    try:
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    except FileNotFoundError:
        return None


def validation_key() -> str:
    """Bind a pass to code, config, interpreter and reconciled distributions.

    No secret values or file contents are persisted; only the final digest is.
    Installation metadata includes timestamps so a same-version reinstall also
    invalidates the cache. Bytecode and test caches are deliberately excluded.
    """
    environment = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", ROOT / ".venv"))
    if not environment.is_absolute():
        environment = ROOT / environment
    files = [environment / "pyvenv.cfg", environment / "bin/python", ROOT / ".env"]
    for site in sorted(environment.glob("lib/python*/site-packages")):
        files.extend(sorted(site.glob("*.dist-info/METADATA")))
        files.extend(sorted(site.glob("*.dist-info/RECORD")))
        files.extend(sorted(site.glob("*.pth")))
    files.append(ROOT / ".cache/toolchains/scrcpy-recorder-fix/build-receipt.json")
    value = {
        "schema": 1,
        "root": str(ROOT),
        "head": git("rev-parse", "HEAD"),
        "updater_sha256": UPDATER_SHA256,
        "tests": TESTS,
        "platform": platform.platform(),
        "python": sys.version,
        "uv": file_identity(Path(shutil.which("uv"))),
        "files": [file_identity(p) for p in files],
        "environment": {
            key: os.environ.get(key)
            for key in (
                "PATH",
                "PYTHONPATH",
                "PYTHONHOME",
                "VIRTUAL_ENV",
                "UV_PROJECT_ENVIRONMENT",
                "ARTEMIS_SCRCPY_PATH",
                "SCRCPY_SERVER_PATH",
                "ARTEMIS_MOCK_DRIVER",
                "PYTEST_ADDOPTS",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            )
        },
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def require_clean_checkout() -> None:
    if git("status", "--porcelain"):
        raise ValueError(
            "Working tree has local changes. Commit or preserve them before updating; nothing changed."
        )
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if Path(git("rev-parse", "--git-path", name)).exists():
            raise ValueError("Finish the existing Git operation before updating.")


def update(state_path: Path) -> int:
    require_clean_checkout()
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    try:
        remote = git("config", "--get", f"branch.{branch}.remote")
        merge_ref = git("config", "--get", f"branch.{branch}.merge")
    except subprocess.CalledProcessError as exc:
        raise ValueError("The current branch must track a remote branch.") from exc
    if not remote or not merge_ref.startswith("refs/heads/"):
        raise ValueError("The current branch must track a remote branch.")
    previous = read_state(state_path)
    # Invalidate even an older matching success before attempting work. Failed
    # syncs, builds, tests and interrupted runs must not inherit that success.
    save_state(state_path, status="pending")
    try:
        print(f"Fetching {remote} {merge_ref}...", flush=True)
        subprocess.run(["git", "fetch", remote, merge_ref], check=True)
        subprocess.run(["git", "merge", "--ff-only", "FETCH_HEAD"], check=True)
        if not shutil.which("uv"):
            raise RuntimeError("uv is required for dependency checks and tests.")
        # Reconciliation is cheap when unchanged and repairs failed syncs.
        subprocess.run(["uv", "sync", "--dev", "--locked"], check=True)
        subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "--no-sync",
                "python",
                "scripts/build_scrcpy_recorder_fix.py",
                "--ensure",
                "--activate",
            ],
            check=True,
        )
        require_clean_checkout()
        key = validation_key()
        cached = (
            previous.get("schema") == 1
            and previous.get("status") == "passed"
            and previous.get("key") == key
        )
        if cached:
            print(
                "Code, environment and scrcpy unchanged; previous validation passed. Skipping repeated tests.",
                flush=True,
            )
        else:
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "--no-sync",
                    "python",
                    "-m",
                    "pytest",
                    *TESTS,
                ],
                check=True,
            )
            # A test must not validate one input and cache a different one.
            if validation_key() != key:
                raise RuntimeError(
                    "Validation inputs changed during tests; success was not cached."
                )
        require_clean_checkout()
        now = datetime.now(UTC).isoformat()
        save_state(
            state_path,
            status="passed",
            key=key,
            checked_at=now,
            validated_at=previous.get("validated_at") if cached else now,
        )
        print("Artemis tracked branch updated and focused checks passed.", flush=True)
        return 0
    except BaseException:
        save_state(state_path, status="failed")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    os.chdir(ROOT)
    lock_path = Path(git("rev-parse", "--git-path", "artemis-update.lock"))
    state_path = Path(git("rev-parse", "--git-path", "artemis-update-state.json"))
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Artemis update is running; nothing changed.", file=sys.stderr)
            return 75
        try:
            return update(state_path)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        except subprocess.CalledProcessError as exc:
            return exc.returncode if exc.returncode > 0 else 1
        except KeyboardInterrupt:
            return 130
        except (OSError, RuntimeError) as exc:
            print(exc, file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
