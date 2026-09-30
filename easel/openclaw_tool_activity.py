"""Read real tool metadata from current OpenClaw's session-scoped trace store.

Only selected tool-event fields leave SQLite. Prompts, thinking, tool output and
exec arguments are never selected. This reader does not modify Gateway state.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import re
import sqlite3
import time
from pathlib import Path

from .content_workflow import safe_error
from .openclaw_workspace import state_dir


class OpenClawToolActivity:
    def __init__(self, session_key: str, project_root: Path, db_path: Path | None = None):
        self.session_key = session_key
        self.root = Path(project_root).resolve()
        self.db = Path(db_path) if db_path is not None else state_dir() / "agents/main/agent/openclaw-agent.sqlite"
        self.session_id = None
        self.started_at_ms = int(time.time() * 1000)
        self.cursor = -1
        self.available = False
        self.seen = set()
        self.labels = {}
        try:
            with self._connect() as connection:
                self.session_id = self._session(connection)
                # This checkpoint is taken after Easel acquires the session lock,
                # before it starts this turn. No prior tool events are replayed.
                self.cursor = self._last_seq(connection, self.session_id)
                connection.execute("SELECT session_id,seq,run_id,event_json FROM trajectory_runtime_events LIMIT 0")
            self.available = True
        except (OSError, sqlite3.Error):
            pass

    @contextmanager
    def _connect(self):
        # mode=ro cannot create a missing database; query_only also forbids writes.
        connection = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def _session(self, connection):
        row = connection.execute("SELECT current_session_id FROM session_nodes WHERE session_key=?", (self.session_key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _last_seq(connection, session_id):
        row = connection.execute("SELECT MAX(seq) FROM trajectory_runtime_events WHERE session_id=?", (session_id or "",)).fetchone()
        return row[0] if row and row[0] is not None else -1

    def _path_label(self, path):
        if not isinstance(path, str) or not path or len(path) > 4096 or any(ord(c) < 32 for c in path):
            return ""
        normalized = path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if not parts or any(part == ".." for part in parts):
            return ""
        name = parts[-1]
        if (name.startswith(".env") or re.search(r"(?i)(secret|credential|auth-profile|token|password)", name)
                or safe_error(path, None) != path):
            return "受保护配置文件"
        # Skills may be addressed via the Gateway workspace's linked directory.
        if "skills" in parts:
            index = parts.index("skills") + 1
            if index < len(parts) and parts[index] == "openclaw":
                index += 1
            if index < len(parts):
                return safe_error("Skill " + " / ".join(parts[index:]), 300)
        try:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = self.root / candidate
            relative = candidate.resolve().relative_to(self.root)
            return safe_error(relative.as_posix(), 300)
        except (OSError, ValueError):
            return safe_error(name, 120)

    def poll(self) -> list[str]:
        if not self.available:
            return []
        try:
            with self._connect() as connection:
                session = self._session(connection)
                if not session:
                    return []  # A new session may not be persisted until admission.
                if self.session_id != session:
                    self.session_id, self.cursor = session, -1
                maximum = self._last_seq(connection, session)
                if maximum <= self.cursor:
                    return []
                rows = connection.execute("""
                    SELECT seq, run_id,
                           json_extract(event_json,'$.type'),
                           json_extract(event_json,'$.sessionKey'),
                           json_extract(event_json,'$.data.toolCallId'),
                           json_extract(event_json,'$.data.name'),
                           COALESCE(json_extract(event_json,'$.data.args.path'),
                                    json_extract(event_json,'$.data.args.file_path'),
                                    json_extract(event_json,'$.data.args.filePath')),
                           json_extract(event_json,'$.data.success')
                    FROM trajectory_runtime_events
                    WHERE session_id=? AND seq>? AND seq<=? AND created_at>=? AND json_valid(event_json)
                      AND json_extract(event_json,'$.type') IN ('tool.call','tool.result')
                    ORDER BY seq LIMIT 200
                """, (session, self.cursor, maximum, self.started_at_ms)).fetchall()
                self.cursor = rows[-1][0] if len(rows) == 200 else maximum
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                self.available = False
            return []
        except (OSError, sqlite3.Error):
            self.available = False
            return []
        output = []
        for _, run_id, kind, key, call_id, name, path, success in rows:
            if key not in (None, self.session_key) or not isinstance(call_id, str) or not isinstance(name, str):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name):
                continue
            identity = (session, run_id, call_id)
            marker = (*identity, kind)
            if marker in self.seen:
                continue
            self.seen.add(marker)
            if kind == "tool.call":
                label = self._path_label(path) if name in {"read", "write", "edit", "read_file", "write_file"} else ""
                self.labels[identity] = label
                output.append(f"工具开始：{name}" + (f" · {label}" if label else ""))
            else:
                label = self.labels.get(identity, "")
                state = "完成" if success is True or success == 1 else "失败" if success is False or success == 0 else "返回（状态未标明）"
                output.append(f"工具{state}：{name}" + (f" · {label}" if label else ""))
        return output

    async def relay(self, is_running, emit, *, interval: float = .25):
        """Forward public metadata and drain the last committed events on exit."""
        while True:
            running = is_running()
            for text in await asyncio.to_thread(self.poll):
                emit(text)
            if not self.available:
                emit("当前网关未提供工具执行细节；可继续查看正文和最终结果。")
                return
            if not running:
                return
            await asyncio.sleep(interval)
