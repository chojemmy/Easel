"""Public tool progress over the existing Gateway WebSocket connection.

Subscribe before starting the turn. HTTP binds the exact completion run ID from
its own SSE response. CLI may bind a fresh lifecycle/start for its locked session.
Only selected tool metadata is retained; other streams and tool bodies are ignored.
SQLite is a labelled, deduplicated completion-time fallback, never a live source.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from .gateway_questions import GatewayClient
from .openclaw_tool_activity import OpenClawToolActivity


class OpenClawLiveToolActivity:
    def __init__(self, session_key: str, project_root: Path, *, db_path: Path | None = None,
                 client_factory=None, allow_lifecycle_binding: bool = False):
        self.session_key = session_key
        self.fallback = OpenClawToolActivity(session_key, project_root, db_path)
        self.started_at_ms = self.fallback.started_at_ms
        self.allow_lifecycle_binding = allow_lifecycle_binding
        self.run_id = None
        self.last_seq = -1
        self.pending = []
        self.ready = []
        self.client = None
        self.live = False
        # Reuse the app's already-paired device and endpoint. No new identity,
        # gateway setting, broad tool invocation or model request is introduced.
        client = None
        try:
            client = (client_factory or (lambda: GatewayClient(timeout=3)))()
            client.connect()
            broad = client._rpc("sessions.subscribe", {}) or {}
            scoped = client._rpc("sessions.messages.subscribe", {"key": session_key}) or {}
            if broad.get("subscribed") is not True or scoped.get("subscribed") is not True:
                raise ValueError("Tool event subscription was not established")
            client.ws.settimeout(.2)
            self.client, self.live = client, True
        except Exception:
            if client is not None:
                client.close()

    def bind_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", run_id):
            return
        if self.run_id is not None:
            return  # Never switch runs because of a later unrelated frame.
        self.run_id = self.fallback.expected_run_id = run_id
        pending, self.pending = self.pending, []
        for metadata in pending:
            self.ready.extend(self._render(metadata))

    def _render(self, metadata):
        run_id, seq, kind, call_id, name, path, success = metadata
        if run_id != self.run_id or seq <= self.last_seq:
            return []
        self.last_seq = seq
        if kind == "compaction.start":
            return ["正在压缩项目会话历史，整理上下文后继续答复…"]
        if kind == "compaction.end":
            return ["项目会话历史压缩已结束，正在继续当前任务。"]
        return self.fallback.render_metadata(run_id, kind, call_id, name, path, success)

    def accept(self, frame) -> list[str]:
        if not isinstance(frame, dict) or frame.get("type") != "event" or frame.get("event") not in {"agent", "session.tool"}:
            return []
        payload = frame.get("payload")
        if not isinstance(payload, dict) or payload.get("sessionKey") != self.session_key:
            return []
        stream, run_id = payload.get("stream"), payload.get("runId")
        if stream not in {"tool", "lifecycle", "compaction"} or not isinstance(run_id, str):
            return []
        if self.run_id is not None and run_id != self.run_id:
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        timestamp = payload.get("ts")
        if isinstance(timestamp, (int, float)) and timestamp < self.started_at_ms:
            return []
        if stream == "lifecycle":
            if (self.allow_lifecycle_binding and self.run_id is None and data.get("phase") == "start"
                    and type(timestamp) in {int, float} and timestamp >= self.started_at_ms):
                self.bind_run(run_id)
            return []
        seq, phase = payload.get("seq"), data.get("phase")
        if type(seq) is not int or seq < 0:
            return []
        if stream == "compaction":
            if phase not in {"start", "end"}:
                return []
            metadata = (run_id, seq, "compaction." + phase, None, None, None, None)
            if self.run_id is None:
                if len(self.pending) < 200:
                    self.pending.append(metadata)
                return []
            return self._render(metadata)
        if phase not in {"start", "result"}:
            return []
        # Do not retain arbitrary args, result, partialResult, text or reasoning.
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        path = args.get("path") or args.get("file_path") or args.get("filePath")
        is_error = data.get("isError")
        success = not is_error if type(is_error) is bool else None
        metadata = (run_id, seq, "tool.call" if phase == "start" else "tool.result",
                    data.get("toolCallId"), data.get("name"), path, success)
        if self.run_id is None:
            if len(self.pending) < 200:
                self.pending.append(metadata)
            return []
        return self._render(metadata)

    def _receive(self):
        try:
            raw = self.client.ws.recv()
        except TimeoutError:
            return None
        except Exception as exc:
            # websocket-client's timeout does not inherit builtin TimeoutError.
            if type(exc).__name__ == "WebSocketTimeoutException":
                return None
            raise
        if not raw:
            raise ConnectionError("Tool event connection closed")
        if not isinstance(raw, (str, bytes)) or len(raw) > 2_000_000:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            return None

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        self.live = False

    async def relay(self, is_running, emit):
        unavailable_reported = False
        if self.live:
            emit("已连接网关实时工具活动。")
        try:
            while is_running():
                for text in self.ready:
                    emit(text)
                self.ready.clear()
                if self.live:
                    try:
                        frame = await asyncio.to_thread(self._receive)
                    except Exception:
                        await asyncio.to_thread(self.close)
                    else:
                        for text in self.accept(frame):
                            emit(text)
                        continue
                if not unavailable_reported:
                    emit("实时工具活动暂不可用；结束后将尝试补全实际执行记录。")
                    unavailable_reported = True
                await asyncio.sleep(.2)
            # A final SSE may beat the last WS frame. Drain already-arriving tool
            # metadata before the supervisor sends done and releases its lock.
            deadline = time.monotonic() + .5
            while self.live and time.monotonic() < deadline:
                try:
                    frame = await asyncio.to_thread(self._receive)
                except Exception:
                    await asyncio.to_thread(self.close)
                    break
                if frame is None:
                    break
                for text in self.accept(frame):
                    emit(text)
            for text in self.ready:
                emit(text)
            self.ready.clear()
            # No bound run means we cannot safely claim any stored tool belongs
            # to this request. Do not replay another run from the same session.
            if self.run_id is not None:
                for text in await asyncio.to_thread(self.fallback.poll):
                    emit("执行记录补全 · " + text)
        finally:
            await asyncio.to_thread(self.close)
