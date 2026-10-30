# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Maintenance scripts preserve user work and expose subprocess failures."""

from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/update_artemis.sh"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo, message):
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "checkout"
    git(tmp_path, "clone", str(remote), str(repo))
    git(repo, "checkout", "-b", "codex/maintenance")
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts/update_artemis.sh")
    (repo / "work.txt").write_text("initial")
    (repo / "uv.lock").write_text("initial lock")
    commit(repo, "initial")
    git(repo, "remote", "rename", "origin", "personal")
    git(repo, "push", "-u", "personal", "HEAD")
    producer = tmp_path / "producer"
    git(tmp_path, "clone", "--branch", "codex/maintenance", str(remote), str(producer))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$UV_CALL_LOG"
if [[ "$1" == sync ]]; then exit "${SYNC_EXIT:-0}"; fi
exit "${TEST_EXIT:-0}"
""")
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "UV_CALL_LOG": str(tmp_path / "uv-calls"),
    }
    return SimpleNamespace(repo=repo, producer=producer, env=env, log=tmp_path / "uv-calls")


def update(c, *args):
    return subprocess.run(
        ["bash", str(c.repo / "scripts/update_artemis.sh"), *args],
        env=c.env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def publish(c, path="work.txt"):
    (c.producer / path).write_text("remote update")
    head = commit(c.producer, "remote update")
    git(c.producer, "push", "origin", "HEAD")
    return head


def test_dirty_work_is_preserved_before_any_fetch(checkout):
    c = checkout
    publish(c)
    (c.repo / "work.txt").write_text("staged")
    git(c.repo, "add", "work.txt")
    (c.repo / "work.txt").write_text("unstaged")
    (c.repo / "untracked.txt").write_text("untracked")
    before = git(c.repo, "status", "--porcelain")
    tracked_head = git(c.repo, "rev-parse", "personal/codex/maintenance")
    result = update(c)
    assert result.returncode == 2
    assert git(c.repo, "status", "--porcelain") == before
    assert git(c.repo, "show", ":work.txt") == "staged"
    assert (c.repo / "work.txt").read_text() == "unstaged"
    assert (c.repo / "untracked.txt").read_text() == "untracked"
    assert git(c.repo, "stash", "list") == ""
    assert git(c.repo, "rev-parse", "personal/codex/maintenance") == tracked_head
    assert not c.log.exists()


def test_fast_forwards_configured_tracking_branch(checkout):
    c = checkout
    head = publish(c)
    result = update(c)
    assert result.returncode == 0, result.stderr
    assert git(c.repo, "rev-parse", "HEAD") == head
    assert git(c.repo, "branch", "--show-current") == "codex/maintenance"
    calls = c.log.read_text().splitlines()
    assert calls[0] == "sync --dev --locked"
    assert calls[1].startswith("run --locked --no-sync python -m pytest ")


def test_divergence_keeps_local_commit_and_files(checkout):
    c = checkout
    publish(c)
    (c.repo / "work.txt").write_text("local commit")
    head = commit(c.repo, "local change")
    result = update(c)
    assert result.returncode != 0
    assert git(c.repo, "rev-parse", "HEAD") == head
    assert (c.repo / "work.txt").read_text() == "local commit"
    assert git(c.repo, "status", "--porcelain") == ""
    assert not c.log.exists()


def test_fetch_failure_keeps_checkout(checkout):
    c = checkout
    head = git(c.repo, "rev-parse", "HEAD")
    git(c.repo, "remote", "set-url", "personal", str(c.repo / "missing.git"))
    assert update(c).returncode != 0
    assert git(c.repo, "rev-parse", "HEAD") == head
    assert git(c.repo, "status", "--porcelain") == ""
    assert not c.log.exists()


@pytest.mark.parametrize("sync_exit", [0, 19])
def test_dependency_sync_and_failure_propagation(checkout, sync_exit):
    c = checkout
    head = publish(c, "uv.lock")
    c.env["SYNC_EXIT"] = str(sync_exit)
    result = update(c)
    assert result.returncode == sync_exit
    assert git(c.repo, "rev-parse", "HEAD") == head
    calls = c.log.read_text().splitlines()
    assert calls[0] == "sync --dev --locked"
    assert len(calls) == (1 if sync_exit else 2)
    assert ("checks passed" in result.stdout) == (sync_exit == 0)


def test_retry_after_failed_sync_still_checks_dependencies(checkout):
    c = checkout
    publish(c, "uv.lock")
    c.env["SYNC_EXIT"] = "19"
    assert update(c).returncode == 19
    c.env["SYNC_EXIT"] = "0"
    assert update(c).returncode == 0
    calls = c.log.read_text().splitlines()
    assert calls[0] == "sync --dev --locked"
    assert calls[1] == "sync --dev --locked"
    assert calls[2].startswith("run --locked --no-sync python -m pytest ")


def test_test_failure_is_not_success(checkout):
    c = checkout
    c.env["TEST_EXIT"] = "23"
    result = update(c)
    assert result.returncode == 23
    assert "checks passed" not in result.stdout


def test_help_needs_no_git_operation(checkout):
    c = checkout
    (c.repo / "work.txt").write_text("dirty")
    assert update(c, "--help").returncode == 0
    assert (c.repo / "work.txt").read_text() == "dirty"
    assert not c.log.exists()


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location(
        "check_local_mcp", ROOT / "scripts/check_local_mcp.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.asyncio
async def test_fresh_mcp_process_result_and_failure(helper, monkeypatch, capsys, is_error):
    result = SimpleNamespace(structuredContent={"ok": not is_error}, isError=is_error)
    session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(return_value=result))
    captured = []

    @asynccontextmanager
    async def transport(params):
        captured.append(params)
        yield (None, None)

    @asynccontextmanager
    async def client(*_):
        yield session

    monkeypatch.setattr(helper, "stdio_client", transport)
    monkeypatch.setattr(helper, "ClientSession", client)
    if is_error:
        with pytest.raises(SystemExit) as exc:
            await helper.call_tool("mobile_diagnose", {"verify_credentials": True})
        assert exc.value.code == 1
    else:
        await helper.call_tool("mobile_diagnose", {"verify_credentials": True})
    assert json.loads(capsys.readouterr().out) == {"ok": not is_error}
    assert captured[0].command == sys.executable
    assert captured[0].cwd == str(ROOT)
    session.initialize.assert_awaited_once()
    session.call_tool.assert_awaited_once_with("mobile_diagnose", {"verify_credentials": True})


def test_invalid_mcp_arguments_never_start_process(helper, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_local_mcp.py", "mobile_diagnose", "[]"])
    monkeypatch.setattr(
        helper, "asyncio", SimpleNamespace(run=lambda _: pytest.fail("must not start"))
    )
    with pytest.raises(SystemExit) as exc:
        helper.main()
    assert exc.value.code == 2
