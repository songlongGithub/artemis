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

"""Decoupled trace retention, inline-image deduplication, and storage cleanup hooks.

Keeps traces/ bounded by enforcing a rolling retention window (default: 2 days)
before task execution and daemon startup, stripping duplicate inline base64 screenshots
from recorded LLM call trace payloads, and disabling eager full-database replay chunk
cloning on server launch.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any
import uuid

from artemis.runtime.process_probe import pid_is_alive

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 2.0
DEFAULT_PRUNE_INTERVAL_SECONDS = 3600.0
MARKER_FILENAME = ".last_retention_prune"
_PRUNE_LOCK = threading.Lock()


def _is_uuid_dir_name(val: str) -> bool:
    parts = val.split("_")
    target = parts[0] if parts else val
    try:
        uuid.UUID(target)
        return True
    except ValueError:
        return False


def strip_inline_images(obj: Any) -> Any:
    """Replace large inline base64 image data URLs or raw byte blobs with compact hashes."""
    if isinstance(obj, dict):
        return {k: strip_inline_images(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_inline_images(v) for v in obj]
    if isinstance(obj, bytes):
        return f"<Bytes length={len(obj)} sha256={hashlib.sha256(obj).hexdigest()[:8]}>"
    if isinstance(obj, str) and len(obj) > 1000:
        if obj.startswith("data:image/") or "base64," in obj[:128]:
            clean_str = obj.split("base64,", 1)[-1].strip()
            padded = clean_str + ("=" * (-len(clean_str) % 4))
            try:
                img_bytes = base64.b64decode(padded, validate=False)
            except (ValueError, TypeError):
                img_bytes = clean_str.encode("utf-8", errors="ignore")
            if len(img_bytes) > 512:
                sha = hashlib.sha256(img_bytes).hexdigest()
                return f"<ImageRef: sha256={sha} length={len(obj)}>"
    return obj


def _resolve_retention_days(retention_days: float | None = None) -> float:
    if retention_days is not None:
        return max(0.0, float(retention_days))
    env_val = os.environ.get("ARTEMIS_RETENTION_DAYS")
    if env_val is not None:
        try:
            return max(0.0, float(env_val))
        except ValueError:
            pass
    return DEFAULT_RETENTION_DAYS


def _resolve_prune_interval(min_interval_seconds: float | None = None) -> float:
    if min_interval_seconds is not None:
        return max(0.0, float(min_interval_seconds))
    env_val = os.environ.get("ARTEMIS_RETENTION_CHECK_INTERVAL_SECONDS")
    if env_val is not None:
        try:
            return max(0.0, float(env_val))
        except ValueError:
            pass
    return DEFAULT_PRUNE_INTERVAL_SECONDS


def _is_session_actively_running(
    base_trace_dir: Path,
    session_id: str,
    status: str | None,
    pid: int | None,
    start_time: float | None,
    cutoff_ts: float,
) -> bool:
    norm_status = str(status or "").strip().lower()
    if norm_status in {"running", "queued", "pending"}:
        if start_time is not None and float(start_time) >= cutoff_ts:
            return True
        if isinstance(pid, int) and pid > 0 and pid_is_alive(pid):
            return True
    status_file = base_trace_dir / session_id / "status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            st = str(data.get("status") or "").strip().lower()
            f_pid = data.get("pid")
            if st in {"running", "pending"} and isinstance(f_pid, int) and f_pid > 0:
                if pid_is_alive(f_pid):
                    return True
        except Exception:
            pass
    return False


def prune_expired_sessions(
    db_path: str | Path,
    base_trace_dir: str | Path,
    *,
    retention_days: float | None = None,
    now: float | None = None,
    sanitize_kept_llm_traces: bool = True,
) -> dict[str, Any]:
    """Delete sessions older than retention_days and compact SQLite + filesystem traces."""
    db_file = Path(db_path)
    trace_root = Path(base_trace_dir)
    days = _resolve_retention_days(retention_days)
    current_time = float(now if now is not None else time.time())
    cutoff_ts = current_time - (days * 86400.0)

    summary: dict[str, Any] = {
        "retention_days": days,
        "cutoff_timestamp": cutoff_ts,
        "deleted_sessions": 0,
        "kept_sessions": 0,
        "sanitized_llm_traces": 0,
        "deleted_session_dirs": 0,
        "deleted_video_dirs": 0,
        "deleted_replay_dirs": 0,
        "deleted_image_files": 0,
        "vacuumed": False,
    }

    if not db_file.exists():
        return summary

    kept_session_ids: set[str] = set()
    expired_session_ids: set[str] = set()
    kept_video_dirs: set[str] = set()
    kept_image_names: set[str] = set()
    db_mutated = False

    conn = sqlite3.connect(str(db_file), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys = OFF")

        existing_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sessions" not in existing_tables:
            return summary

        rows = conn.execute(
            "SELECT session_id, status, start_time, end_time, pid FROM sessions"
        ).fetchall()
        for row in rows:
            sid = str(row["session_id"] or "")
            if not sid:
                continue
            st = row["status"]
            start_t = float(row["start_time"] or 0.0)
            end_t = float(row["end_time"] or 0.0)
            last_t = max(start_t, end_t)
            pid = row["pid"]
            if last_t >= cutoff_ts or _is_session_actively_running(
                trace_root, sid, st, pid, start_t, cutoff_ts
            ):
                kept_session_ids.add(sid)
            else:
                expired_session_ids.add(sid)

        summary["kept_sessions"] = len(kept_session_ids)
        summary["deleted_sessions"] = len(expired_session_ids)

        session_tables = (
            "failed_outputs",
            "traces",
            "background_tasks",
            "history_chunks",
            "video_recordings",
            "steps",
            "sessions",
        )

        if kept_session_ids:
            kept_list = sorted(kept_session_ids)
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _kept_sessions (session_id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _kept_sessions")
            conn.executemany(
                "INSERT INTO _kept_sessions (session_id) VALUES (?)",
                [(s,) for s in kept_list],
            )

            if "video_recordings" in existing_tables:
                kept_videos = conn.execute(
                    "SELECT video_id, local_video_path FROM video_recordings "
                    "WHERE session_id IN (SELECT session_id FROM _kept_sessions)"
                ).fetchall()
                kept_board_keys = [f"session:{s}" for s in kept_list]
                for v_row in kept_videos:
                    if v_row["video_id"]:
                        kept_board_keys.append(f"video:{v_row['video_id']}")
                    if v_row["local_video_path"]:
                        kept_video_dirs.add(Path(str(v_row["local_video_path"])).parent.name)

                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _kept_boards (board_key TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM _kept_boards")
                conn.executemany(
                    "INSERT OR IGNORE INTO _kept_boards (board_key) VALUES (?)",
                    [(k,) for k in kept_board_keys],
                )
                for v_table in ("video_analysis_observations", "video_analysis_segments"):
                    if v_table in existing_tables:
                        cur = conn.execute(
                            f"DELETE FROM {v_table} WHERE board_key NOT IN (SELECT board_key FROM _kept_boards)"
                        )
                        if cur.rowcount and cur.rowcount > 0:
                            db_mutated = True

            for tbl in session_tables:
                if tbl in existing_tables:
                    cur = conn.execute(
                        f"DELETE FROM {tbl} WHERE session_id NOT IN (SELECT session_id FROM _kept_sessions)"
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        db_mutated = True
        else:
            for tbl in (
                "video_analysis_observations",
                "video_analysis_segments",
                *session_tables,
            ):
                if tbl in existing_tables:
                    cur = conn.execute(f"DELETE FROM {tbl}")
                    if cur.rowcount and cur.rowcount > 0:
                        db_mutated = True

        if "images" in existing_tables and "steps" in existing_tables:
            cur = conn.execute(
                """
                DELETE FROM images
                WHERE image_name NOT IN (
                    SELECT pre_image_name FROM steps WHERE pre_image_name IS NOT NULL
                    UNION
                    SELECT post_image_name FROM steps WHERE post_image_name IS NOT NULL
                )
                """
            )
            if cur.rowcount and cur.rowcount > 0:
                db_mutated = True
            kept_image_names = {
                str(r[0])
                for r in conn.execute("SELECT image_name FROM images").fetchall()
                if r[0]
            }

        if sanitize_kept_llm_traces and "traces" in existing_tables:
            bloated_rows = conn.execute(
                "SELECT trace_id, payload FROM traces "
                "WHERE type = 'llm_call' AND instr(payload, 'base64,') > 0"
            ).fetchall()
            updates: list[tuple[str, str]] = []
            for b_row in bloated_rows:
                t_id = str(b_row["trace_id"])
                raw_payload = b_row["payload"]
                if not raw_payload:
                    continue
                try:
                    parsed = json.loads(raw_payload)
                except Exception:
                    continue
                cleaned = strip_inline_images(parsed)
                new_payload = json.dumps(cleaned, ensure_ascii=False)
                if len(new_payload) < len(raw_payload):
                    updates.append((new_payload, t_id))
            if updates:
                conn.executemany(
                    "UPDATE traces SET payload = ? WHERE trace_id = ?",
                    updates,
                )
                summary["sanitized_llm_traces"] = len(updates)
                db_mutated = True

        conn.commit()

        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if db_mutated or freelist > 500:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
            summary["vacuumed"] = True
    finally:
        conn.close()

    if trace_root.exists():
        kept_short_sids = {sid.split("-")[0] for sid in kept_session_ids if sid}

        replay_dir = trace_root / "replay"
        if replay_dir.exists():
            replay_data_dir = replay_dir / "data"
            if replay_data_dir.exists():
                older_dir = replay_data_dir / "older"
                if older_dir.exists():
                    shutil.rmtree(older_dir, ignore_errors=True)
                    summary["deleted_replay_dirs"] += 1
                for item in replay_data_dir.iterdir():
                    if not item.is_dir():
                        continue
                    sid_prefix = (
                        item.name[: -len("_chunked")]
                        if item.name.endswith("_chunked")
                        else item.name
                    )
                    should_remove = sid_prefix not in kept_session_ids
                    if not should_remove:
                        for tj in item.glob("step_*/traces.json"):
                            try:
                                if tj.stat().st_size > 2 * 1024 * 1024:
                                    should_remove = True
                                    break
                            except OSError:
                                pass
                    if should_remove:
                        shutil.rmtree(item, ignore_errors=True)
                        summary["deleted_replay_dirs"] += 1

            replay_outputs_dir = replay_dir / "outputs"
            if replay_outputs_dir.exists():
                for item in replay_outputs_dir.iterdir():
                    if item.is_dir() and item.name not in kept_session_ids:
                        shutil.rmtree(item, ignore_errors=True)
                        summary["deleted_replay_dirs"] += 1

        for path in trace_root.iterdir():
            if not path.is_dir() or path.name in {"images", "replay"}:
                continue
            if path.name.startswith("web_"):
                parts = path.name.split("_")
                short_sid = parts[2] if len(parts) >= 3 else ""
                is_kept = (
                    path.name in kept_video_dirs
                    or any(path.name.startswith(k) for k in kept_video_dirs if k)
                    or (short_sid and short_sid in kept_short_sids)
                )
                if not is_kept:
                    try:
                        if (
                            any(sid.startswith(short_sid) for sid in expired_session_ids if short_sid)
                            or path.stat().st_mtime < cutoff_ts
                        ):
                            shutil.rmtree(path, ignore_errors=True)
                            summary["deleted_video_dirs"] += 1
                    except OSError:
                        pass
            elif _is_uuid_dir_name(path.name):
                if path.name in expired_session_ids:
                    shutil.rmtree(path, ignore_errors=True)
                    summary["deleted_session_dirs"] += 1
                elif path.name not in kept_session_ids:
                    try:
                        if path.stat().st_mtime < cutoff_ts:
                            shutil.rmtree(path, ignore_errors=True)
                            summary["deleted_session_dirs"] += 1
                    except OSError:
                        pass

        images_dir = trace_root / "images"
        if images_dir.exists():
            for img_file in images_dir.iterdir():
                if not img_file.is_file():
                    continue
                if img_file.stem not in kept_image_names:
                    try:
                        if img_file.stat().st_mtime < current_time - 60.0:
                            img_file.unlink()
                            summary["deleted_image_files"] += 1
                    except OSError:
                        pass

    return summary


def ensure_retention_pruned(
    db_path: str | Path | None = None,
    base_trace_dir: str | Path | None = None,
    *,
    retention_days: float | None = None,
    min_interval_seconds: float | None = None,
    force: bool = False,
    allow_under_pytest: bool = False,
) -> dict[str, Any] | None:
    """Throttled pre-flight check ensuring only the latest N days of traces are kept."""
    if os.environ.get("ARTEMIS_AUTO_PRUNE", "1") == "0" and not force:
        return None
    if "PYTEST_CURRENT_TEST" in os.environ and not allow_under_pytest:
        return None

    if base_trace_dir is None:
        from artemis.runtime import trace_store

        trace_root = Path(trace_store.TRACES_DIR)
    else:
        trace_root = Path(base_trace_dir)

    if db_path is None:
        db_file = trace_root / "data_engine.db"
    else:
        db_file = Path(db_path)

    if not db_file.exists():
        return None

    interval = _resolve_prune_interval(min_interval_seconds)
    marker = trace_root / MARKER_FILENAME
    now = time.time()

    with _PRUNE_LOCK:
        if not force and interval > 0 and marker.exists():
            try:
                if now - marker.stat().st_mtime < interval:
                    return None
            except OSError:
                pass
        try:
            trace_root.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{now:.3f}\n", encoding="utf-8")
        except OSError:
            pass

    try:
        return prune_expired_sessions(
            db_file,
            trace_root,
            retention_days=retention_days,
            now=now,
        )
    except Exception as exc:
        logger.debug("Retention prune skipped due to error: %s", exc, exc_info=True)
        return None


def install_retention_hooks(*, include_data_engine: bool = False) -> None:
    """Install non-intrusive runtime wrappers to avoid upstream merge conflicts."""
    try:
        from artemis.runtime import trace_store

        orig_init_trace = trace_store.init_trace
        if not getattr(orig_init_trace, "_retention_wrapped", False):

            @functools.wraps(orig_init_trace)
            def _wrapped_init_trace(*args: Any, **kwargs: Any) -> Any:
                ensure_retention_pruned(base_trace_dir=trace_store.TRACES_DIR)
                return orig_init_trace(*args, **kwargs)

            _wrapped_init_trace._retention_wrapped = True  # type: ignore[attr-defined]
            trace_store.init_trace = _wrapped_init_trace
    except Exception as exc:
        logger.debug("Could not wrap trace_store.init_trace: %s", exc)

    if not include_data_engine:
        return

    try:
        from artemis.data_engine.trace import DataEngineCallbackHandler

        orig_serialize = DataEngineCallbackHandler._serialize_message
        if not getattr(orig_serialize, "_retention_wrapped", False):

            @functools.wraps(orig_serialize)
            def _wrapped_serialize(self: Any, msg: Any) -> dict[str, Any]:
                raw = orig_serialize(self, msg)
                return strip_inline_images(raw)

            _wrapped_serialize._retention_wrapped = True  # type: ignore[attr-defined]
            DataEngineCallbackHandler._serialize_message = _wrapped_serialize  # type: ignore[method-assign]
    except Exception as exc:
        logger.debug("Could not wrap DataEngineCallbackHandler: %s", exc)

    try:
        from artemis.data_engine.storage import StorageManager

        StorageManager.prune_expired_sessions = lambda self, **kw: prune_expired_sessions(  # type: ignore[attr-defined]
            self.db_path, self.base_trace_dir, **kw
        )

        orig_storage_init = StorageManager.__init__
        if not getattr(orig_storage_init, "_retention_wrapped", False):

            @functools.wraps(orig_storage_init)
            def _wrapped_storage_init(
                self: Any,
                db_path: str | Path,
                base_trace_dir: str | Path,
                *,
                read_only: bool = False,
            ) -> None:
                orig_storage_init(self, db_path, base_trace_dir, read_only=read_only)
                if (
                    not read_only
                    and self.db_path.name == "data_engine.db"
                    and "temp_traces" not in self.db_path.parts
                ):
                    ensure_retention_pruned(self.db_path, self.base_trace_dir)

            _wrapped_storage_init._retention_wrapped = True  # type: ignore[attr-defined]
            StorageManager.__init__ = _wrapped_storage_init  # type: ignore[method-assign]

        orig_clear_all = StorageManager.clear_all_data
        if not getattr(orig_clear_all, "_retention_wrapped", False):

            @functools.wraps(orig_clear_all)
            def _wrapped_clear_all(self: Any) -> None:
                with self._get_connection() as conn:
                    for extra_tbl in ("history_chunks", "video_recordings"):
                        try:
                            conn.execute(f"DELETE FROM {extra_tbl}")
                        except sqlite3.OperationalError:
                            pass
                    conn.commit()
                orig_clear_all(self)
                replay_dir = self.base_trace_dir / "replay"
                if replay_dir.exists():
                    shutil.rmtree(replay_dir, ignore_errors=True)
                with self._get_connection() as conn:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.OperationalError:
                        pass

            _wrapped_clear_all._retention_wrapped = True  # type: ignore[attr-defined]
            StorageManager.clear_all_data = _wrapped_clear_all  # type: ignore[method-assign]

        orig_delete_session = StorageManager.delete_session
        if not getattr(orig_delete_session, "_retention_wrapped", False):

            @functools.wraps(orig_delete_session)
            def _wrapped_delete_session(self: Any, session_id: uuid.UUID) -> None:
                sid_str = str(session_id)
                with self._get_connection() as conn:
                    try:
                        conn.execute("DELETE FROM history_chunks WHERE session_id = ?", (sid_str,))
                        conn.commit()
                    except sqlite3.OperationalError:
                        pass
                orig_delete_session(self, session_id)
                chunk_dir = self.base_trace_dir / "replay" / "data" / f"{sid_str}_chunked"
                if chunk_dir.exists():
                    shutil.rmtree(chunk_dir, ignore_errors=True)

            _wrapped_delete_session._retention_wrapped = True  # type: ignore[attr-defined]
            StorageManager.delete_session = _wrapped_delete_session  # type: ignore[method-assign]
    except Exception as exc:
        logger.debug("Could not wrap StorageManager: %s", exc)

    try:
        from apps.admin_console.replay_manager import ReplayManager

        orig_ensure_chunked = ReplayManager._ensure_session_chunked
        if not getattr(orig_ensure_chunked, "_retention_wrapped", False):

            @functools.wraps(orig_ensure_chunked)
            def _wrapped_ensure_chunked(self: Any, session_id: str) -> Path:
                for frame_info in inspect.stack()[1:4]:
                    if frame_info.function == "verify_chunks_exist_on_launch":
                        return self.test_data_dir / f"{session_id}_chunked"
                return orig_ensure_chunked(self, session_id)

            _wrapped_ensure_chunked._retention_wrapped = True  # type: ignore[attr-defined]
            ReplayManager._ensure_session_chunked = _wrapped_ensure_chunked  # type: ignore[method-assign]
    except Exception:
        pass


install_retention_hooks(include_data_engine=True)
