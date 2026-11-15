"""Native cache/activation policy tests; compilation is verified separately."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def builder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "scrcpy_builder", ROOT / "scripts/build_scrcpy_recorder_fix.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    patch = tmp_path / "recorder.patch"
    patch.write_text("patch version one")
    recipe = tmp_path / "builder.py"
    recipe.write_text("recipe version one")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PATCH", patch)
    monkeypatch.setattr(module, "__file__", str(recipe))
    monkeypatch.setattr(module, "SERVER_SHA256", hashlib.sha256(b"server").hexdigest())
    prefix = tmp_path / "install"
    binary = prefix / "bin/scrcpy"
    server = prefix / "share/scrcpy/scrcpy-server"
    binary.parent.mkdir(parents=True)
    server.parent.mkdir(parents=True)
    binary.write_bytes(b"client")
    binary.chmod(0o755)
    server.write_bytes(b"server")
    receipt = {
        **module.build_inputs(),
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(b"client").hexdigest(),
        "native_tests_passed": True,
    }
    (prefix / "build-receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_scrcpy_recorder_fix.py", "--ensure", "--activate", "--prefix", str(prefix)],
    )
    return module, prefix


def test_matching_build_reuses_binary_and_repairs_activation(builder, monkeypatch):
    module, prefix = builder
    build = Mock(side_effect=AssertionError("must not rebuild unchanged inputs"))
    monkeypatch.setattr(module, "build", build)
    (module.ROOT / ".env").write_text("UNRELATED=preserved\nARTEMIS_SCRCPY_PATH=old-client\n")
    module.main()
    from dotenv import dotenv_values

    values = dotenv_values(module.ROOT / ".env")
    assert values["UNRELATED"] == "preserved"
    assert values["ARTEMIS_SCRCPY_PATH"] == str(prefix / "bin/scrcpy")
    assert values["SCRCPY_SERVER_PATH"] == str(prefix / "share/scrcpy/scrcpy-server")
    before = (module.ROOT / ".env").stat().st_mtime_ns
    module.main()
    assert (module.ROOT / ".env").stat().st_mtime_ns == before
    build.assert_not_called()


@pytest.mark.parametrize(
    "damage",
    [
        "patch",
        "recipe",
        "client",
        "server",
        "missing-client",
        "not-executable",
        "receipt",
        "failed-tests",
        "legacy-receipt",
        "platform",
    ],
)
def test_changed_or_invalid_native_installation_is_rebuilt(builder, monkeypatch, damage):
    module, prefix = builder
    path = prefix / "build-receipt.json"
    receipt = json.loads(path.read_text())
    if damage == "patch":
        module.PATCH.write_text("patch version two")
    elif damage == "recipe":
        Path(module.__file__).write_text("recipe version two")
    elif damage == "client":
        (prefix / "bin/scrcpy").write_bytes(b"broken client")
    elif damage == "server":
        (prefix / "share/scrcpy/scrcpy-server").write_bytes(b"wrong server")
    elif damage == "missing-client":
        (prefix / "bin/scrcpy").unlink()
    elif damage == "not-executable":
        (prefix / "bin/scrcpy").chmod(0o644)
    elif damage == "receipt":
        path.write_text("corrupt JSON")
    else:
        if damage == "failed-tests":
            receipt["native_tests_passed"] = False
        elif damage == "legacy-receipt":
            receipt.pop("builder_sha256")
        elif damage == "platform":
            receipt["platform"] = "different host"
        path.write_text(json.dumps(receipt))
    rebuild = Mock(return_value={"rebuilt": True})
    activate = Mock()
    monkeypatch.setattr(module, "build", rebuild)
    monkeypatch.setattr(module, "activate", activate)
    module.main()
    rebuild.assert_called_once_with(prefix)
    activate.assert_called_once_with(prefix)


def test_failed_rebuild_never_activates(builder, monkeypatch):
    module, prefix = builder
    module.PATCH.write_text("new patch")
    activate = Mock()
    monkeypatch.setattr(module, "activate", activate)
    monkeypatch.setattr(
        module, "build", Mock(side_effect=subprocess.CalledProcessError(17, "meson"))
    )
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    activate.assert_not_called()
    assert not module.current_build(prefix)


def test_publication_keeps_open_old_binary_intact(builder, tmp_path):
    module, prefix = builder
    staged = tmp_path / "staged/bin"
    staged.mkdir(parents=True)
    (staged / "scrcpy").write_bytes(b"replacement client")
    (staged / "scrcpy").chmod(0o755)
    server = staged.parent / "share/scrcpy/scrcpy-server"
    server.parent.mkdir(parents=True)
    server.write_bytes(b"replacement server")
    with (prefix / "bin/scrcpy").open("rb") as running_binary:
        module.publish_installation(staged.parent, prefix)
        assert running_binary.read() == b"client"
    assert (prefix / "bin/scrcpy").read_bytes() == b"replacement client"
    assert os.access(prefix / "bin/scrcpy", os.X_OK)


@pytest.mark.parametrize("explicit", [False, True])
def test_start_script_prefers_dotenv_selection_to_legacy_default(tmp_path, explicit):
    """Execute the exact startup selection block with a competing old install."""
    source = (ROOT / "start.sh").read_text()
    block = source[
        source.index("# Prefer the managed selection in .env") : source.index("# 6. Check")
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!{sys.executable}\nimport os\nprint(os.environ["SELECTED_SERVER"] if "SCRCPY_SERVER_PATH" in __import__("sys").argv[-1] else os.environ["SELECTED_CLIENT"])\n'
    )
    uv.chmod(0o755)
    legacy = tmp_path / "legacy/bin/scrcpy"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old client")
    legacy.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ARTEMIS_SCRCPY_PATH", "SCRCPY_SERVER_PATH")
    }
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        ARTEMIS_SCRCPY_PATCHED_DIR=str(legacy.parent.parent),
        SELECTED_CLIENT="/managed/client",
        SELECTED_SERVER="/managed/server",
    )
    if explicit:
        env.update(ARTEMIS_SCRCPY_PATH="/explicit/client", SCRCPY_SERVER_PATH="/explicit/server")
    script = (
        'set -eu\nGREEN=""; NC=""\n'
        + block
        + '\nprintf "SELECTED=%s,%s\\n" "$ARTEMIS_SCRCPY_PATH" "$SCRCPY_SERVER_PATH"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert (
        f"SELECTED=/{'explicit' if explicit else 'managed'}/client,/{'explicit' if explicit else 'managed'}/server"
        in result.stdout
    )
