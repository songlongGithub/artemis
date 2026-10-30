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

"""Unit tests for decoupled 2-day trace retention and inline base64 deduplication."""

import base64
import os
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from langchain_core.messages import HumanMessage

from artemis.context import ArtemisContext
from artemis.data_engine.models import ImageRecord, SessionMetadata, StepRecord, TraceRecord
from artemis.data_engine.storage import StorageManager
from artemis.data_engine.trace import DataEngineCallbackHandler
from artemis.runtime.trace_retention import (
    ensure_retention_pruned,
    prune_expired_sessions,
)


def _make_b64_image() -> str:
    raw = b"\xff\xd8\xff\xe0" + (b"A" * 1600)
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def test_callback_handler_strips_inline_base64_images_without_truncating_text():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.current_step_id = uuid4()

    handler = DataEngineCallbackHandler(mock_ctx)
    long_text = "Prompt line " * 600  # > 5000 chars normal text
    b64_url = _make_b64_image()
    msg = HumanMessage(
        content=[
            {"type": "text", "text": long_text},
            {"type": "image_url", "image_url": {"url": b64_url}},
        ]
    )
    handler.on_chat_model_start({"name": "ChatOpenAI"}, [[msg]], run_id=uuid4())

    _, kwargs = mock_ctx.data_engine.record_trace.call_args
    recorded_content = kwargs["payload"]["messages"][0]["content"]
    assert recorded_content[0]["text"] == long_text
    assert recorded_content[1]["image_url"]["url"].startswith("<ImageRef: sha256=")


def test_prune_expired_sessions_keeps_recent_and_active_while_cleaning_expired(tmp_path: Path):
    db_path = tmp_path / "data_engine.db"
    storage = StorageManager(db_path, tmp_path)
    now = 1_800_000_000.0

    old_sid = uuid4()
    recent_sid = uuid4()
    active_old_sid = uuid4()

    storage.create_session(
        SessionMetadata(
            session_id=old_sid,
            initial_goal="old task",
            start_time=now - 3 * 86400,
            end_time=now - 3 * 86400 + 30,
            status="completed",
        )
    )
    storage.create_session(
        SessionMetadata(
            session_id=recent_sid,
            initial_goal="recent task",
            start_time=now - 1 * 86400,
            end_time=now - 1 * 86400 + 30,
            status="completed",
        )
    )
    storage.create_session(
        SessionMetadata(
            session_id=active_old_sid,
            initial_goal="still running",
            start_time=now - 4 * 86400,
            status="running",
            pid=os.getpid(),
        )
    )

    storage.create_image(ImageRecord(image_name="img_old", timestamp=now - 3 * 86400))
    storage.create_image(ImageRecord(image_name="img_recent", timestamp=now - 1 * 86400))
    storage.create_step(
        StepRecord(
            step_id=uuid4(),
            session_id=old_sid,
            step_number=1,
            timestamp=now - 3 * 86400,
            pre_image_name="img_old",
        )
    )
    recent_step_id = uuid4()
    storage.create_step(
        StepRecord(
            step_id=recent_step_id,
            session_id=recent_sid,
            step_number=1,
            timestamp=now - 1 * 86400,
            pre_image_name="img_recent",
        )
    )
    storage.create_trace(
        TraceRecord(
            trace_id=uuid4(),
            session_id=recent_sid,
            step_id=recent_step_id,
            type="llm_call",
            name="ChatOpenAI",
            timestamp=now - 1 * 86400,
            status="success",
            payload={"messages": [{"content": _make_b64_image()}]},
        )
    )

    # Create filesystem artifacts
    images_dir = tmp_path / "images"
    images_dir.mkdir(exist_ok=True)
    old_jpg = images_dir / "img_old.jpg"
    recent_jpg = images_dir / "img_recent.jpg"
    old_jpg.write_bytes(b"old")
    recent_jpg.write_bytes(b"recent")
    os.utime(old_jpg, (now - 3 * 86400, now - 3 * 86400))

    old_sess_dir = tmp_path / str(old_sid)
    old_sess_dir.mkdir(exist_ok=True)
    old_web_dir = tmp_path / f"web_1700000000_{str(old_sid).split('-')[0]}_PASS"
    old_web_dir.mkdir()
    os.utime(old_web_dir, (now - 3 * 86400, now - 3 * 86400))

    replay_chunk = tmp_path / "replay" / "data" / f"{old_sid}_chunked"
    replay_chunk.mkdir(parents=True)

    result = prune_expired_sessions(db_path, tmp_path, retention_days=2.0, now=now)

    assert result["deleted_sessions"] == 1
    assert result["kept_sessions"] == 2
    assert result["sanitized_llm_traces"] == 1
    assert storage.get_session(old_sid) is None
    assert storage.get_session(recent_sid) is not None
    assert storage.get_session(active_old_sid) is not None
    assert not old_jpg.exists()
    assert recent_jpg.exists()
    assert not old_sess_dir.exists()
    assert not old_web_dir.exists()
    assert not replay_chunk.exists()


def test_ensure_retention_pruned_throttles_until_forced(tmp_path: Path):
    db_path = tmp_path / "data_engine.db"
    StorageManager(db_path, tmp_path)

    res1 = ensure_retention_pruned(
        db_path,
        tmp_path,
        min_interval_seconds=3600.0,
        allow_under_pytest=True,
    )
    assert res1 is not None

    res2 = ensure_retention_pruned(
        db_path,
        tmp_path,
        min_interval_seconds=3600.0,
        allow_under_pytest=True,
    )
    assert res2 is None

    res3 = ensure_retention_pruned(
        db_path,
        tmp_path,
        min_interval_seconds=3600.0,
        force=True,
        allow_under_pytest=True,
    )
    assert res3 is not None
