"""Gateway tool stream boundaries; no gateway, model or production imports."""
import asyncio
import json
from pathlib import Path
import queue
import sqlite3
import time

import pytest

from easel.openclaw_live_activity import OpenClawLiveToolActivity


KEY = "agent:main:workflow-example-script"


class Client:
    def __init__(self, fail=False):
        self.ws = self
        self.frames = queue.Queue()
        self.calls = []
        self.closed = False
        self.fail = fail
    def connect(self):
        self.calls.append("connect")
        if self.fail:
            raise ConnectionError("private connection failure")
    def _rpc(self, method, params):
        self.calls.append((method, params))
        return {"subscribed": True}
    def settimeout(self, timeout):
        pass
    def recv(self):
        try:
            item = self.frames.get(timeout=.005)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)
    def close(self):
        self.closed = True


def event(*, key=KEY, run="chatcmpl_own", seq=1, phase="start", name="read", call="call-1", path=None, timestamp=None):
    return {"type": "event", "event": "session.tool", "payload": {
        "sessionKey": key, "runId": run, "seq": seq,
        "ts": timestamp if timestamp is not None else int(time.time() * 1000), "stream": "tool",
        "data": {"phase": phase, "name": name, "toolCallId": call, "isError": False,
                 "args": {"path": path, "command": "PRIVATE COMMAND", "token": "PRIVATE TOKEN"},
                 "result": "PRIVATE TOOL BODY", "partialResult": "PRIVATE PARTIAL"}}}


def adapter(tmp_path, client=None, **kwargs):
    return OpenClawLiveToolActivity(KEY, tmp_path, db_path=tmp_path / "missing.sqlite",
        client_factory=lambda: client or Client(), **kwargs)


def test_subscriptions_acknowledged_before_adapter_returns(tmp_path):
    client = Client()
    feed = adapter(tmp_path, client)
    assert feed.live
    assert client.calls == ["connect", ("sessions.subscribe", {}), ("sessions.messages.subscribe", {"key": KEY})]
    feed.close()


def test_exact_http_run_session_and_sequence_gate_tool_events(tmp_path):
    feed = adapter(tmp_path)
    assert feed.accept(event(run="old-run", seq=1)) == []
    assert feed.accept(event(run="chatcmpl_own", seq=5)) == []
    feed.bind_run("chatcmpl_own")
    assert feed.ready == ["工具开始：read"]
    assert feed.accept(event(key="agent:main:someone-else", seq=6)) == []
    assert feed.accept(event(run="old-run", seq=6)) == []
    assert feed.accept(event(seq=6, phase="result")) == ["工具完成：read"]
    assert feed.accept(event(seq=6, phase="result")) == []
    assert feed.accept(event(seq=4, call="out-of-order")) == []
    assert feed.accept(event(seq=7)) == []  # Duplicate call/phase, new event seq.
    feed.bind_run("new-unrelated-run")
    assert feed.run_id == "chatcmpl_own"
    feed.close()


def test_public_metadata_only_and_no_reasoning_or_tool_output(tmp_path):
    feed = adapter(tmp_path)
    feed.bind_run("chatcmpl_own")
    frame = event(path=str(tmp_path / "skills/openclaw/text-polisher/SKILL.md"))
    assert feed.accept(frame) == ["工具开始：read · Skill text-polisher / SKILL.md"]
    frame["payload"]["stream"] = "thinking"
    frame["payload"]["data"] = {"text": "PRIVATE THINKING"}
    assert feed.accept(frame) == []
    assert feed.accept(event(seq=2, phase="update")) == []
    assert feed.accept(event(seq=3, name="exec", call="exec")) == ["工具开始：exec"]
    assert "PRIVATE" not in repr(feed.pending) + repr(feed.ready) + repr(feed.fallback.labels)
    feed.close()


def test_compaction_progress_is_exact_run_scoped_and_contains_no_summary(tmp_path):
    feed = adapter(tmp_path)
    frame = event(seq=1)
    frame["event"] = "agent"
    frame["payload"]["stream"] = "compaction"
    frame["payload"]["data"] = {"phase": "start", "summary": "PRIVATE SUMMARY", "messages": ["PRIVATE HOOK"]}
    assert feed.accept(frame) == []
    feed.bind_run("chatcmpl_own")
    assert feed.ready == ["正在压缩项目会话历史，整理上下文后继续答复…"]
    frame["payload"]["runId"] = "someone-else"
    frame["payload"]["seq"] = 2
    frame["payload"]["data"]["phase"] = "end"
    assert feed.accept(frame) == []
    frame["payload"]["runId"] = "chatcmpl_own"
    assert feed.accept(frame) == ["项目会话历史压缩已结束，正在继续当前任务。"]
    assert "PRIVATE" not in repr(feed.pending) + repr(feed.ready)
    feed.close()


def test_cli_binds_only_fresh_lifecycle_start(tmp_path):
    feed = adapter(tmp_path, allow_lifecycle_binding=True)
    frame = event(timestamp=0)
    frame["event"] = "agent"
    frame["payload"]["stream"] = "lifecycle"
    assert feed.accept(frame) == [] and feed.run_id is None
    frame["payload"]["ts"] = feed.started_at_ms
    feed.accept(frame)
    assert feed.run_id == "chatcmpl_own"
    assert feed.accept(event(seq=2)) == ["工具开始：read"]
    feed.close()


def test_live_start_and_result_arrive_while_task_is_still_running(tmp_path):
    client = Client()
    feed = adapter(tmp_path, client)
    feed.bind_run("chatcmpl_own")
    client.frames.put(event())
    client.frames.put(event(seq=2, phase="result"))
    running, observed = True, []
    def emit(text):
        nonlocal running
        observed.append((text, running))
        if text == "工具完成：read":
            running = False
    asyncio.run(asyncio.wait_for(feed.relay(lambda: running, emit), timeout=1))
    assert ("工具开始：read", True) in observed
    assert ("工具完成：read", True) in observed
    assert client.closed


def test_last_queued_ws_result_is_drained_before_relay_returns(tmp_path):
    client = Client()
    feed = adapter(tmp_path, client)
    feed.bind_run("chatcmpl_own")
    client.frames.put(event())
    client.frames.put(event(seq=2, phase="result"))
    observed = []
    asyncio.run(feed.relay(lambda: False, observed.append))
    assert observed[-2:] == ["工具开始：read", "工具完成：read"]


def test_disconnect_fallback_is_labelled_run_scoped_and_deduplicated(tmp_path):
    database = tmp_path / "trace.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE session_nodes(session_key TEXT PRIMARY KEY, current_session_id TEXT);
            CREATE TABLE trajectory_runtime_events(session_id TEXT,seq INTEGER,run_id TEXT,event_json TEXT,created_at INTEGER);
        """)
        connection.execute("INSERT INTO session_nodes VALUES(?,?)", (KEY, "session-1"))
    client = Client()
    feed = OpenClawLiveToolActivity(KEY, tmp_path, db_path=database, client_factory=lambda: client)
    feed.bind_run("chatcmpl_own")
    assert feed.accept(event()) == ["工具开始：read"]
    with sqlite3.connect(database) as connection:
        for seq, kind, run in [(0, "tool.call", "chatcmpl_own"), (1, "tool.result", "chatcmpl_own"), (2, "tool.call", "other-run")]:
            entry = {"type": kind, "sessionKey": KEY, "data": {"toolCallId": "call-1", "name": "read", "success": True}}
            connection.execute("INSERT INTO trajectory_runtime_events VALUES(?,?,?,?,?)",
                ("session-1", seq, run, json.dumps(entry), feed.started_at_ms))
    client.frames.put(ConnectionError("PRIVATE FAILURE"))
    observed, running = [], True
    def emit(text):
        nonlocal running
        observed.append(text)
        if "暂不可用" in text:
            running = False
    asyncio.run(feed.relay(lambda: running, emit))
    assert observed[-1] == "执行记录补全 · 工具完成：read"
    assert not any("工具开始" in text or "PRIVATE" in text for text in observed)


def test_no_bound_run_never_replays_fallback(tmp_path, monkeypatch):
    feed = adapter(tmp_path, Client(fail=True))
    monkeypatch.setattr(feed.fallback, "poll", lambda: pytest.fail("Unbound run must not replay stored events"))
    asyncio.run(feed.relay(lambda: False, lambda _: None))


def test_missing_ws_dependency_downgrades_without_leaking_error(tmp_path):
    def missing_client():
        raise ImportError("PRIVATE INSTALL PATH")
    feed = OpenClawLiveToolActivity(KEY, tmp_path, db_path=tmp_path / "missing.sqlite", client_factory=missing_client)
    assert not feed.live


def test_cancellation_closes_observer_connection(tmp_path):
    client = Client()
    feed = adapter(tmp_path, client)
    async def run():
        task = asyncio.create_task(feed.relay(lambda: True, lambda _: None))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert client.closed
