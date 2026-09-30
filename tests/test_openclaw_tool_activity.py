"""Real SQLite trace schema, without opening or mutating production state."""
import asyncio
import json
import sqlite3

import pytest

from easel.openclaw_tool_activity import OpenClawToolActivity


KEY = "agent:main:workflow-example-script"


@pytest.fixture
def trace(tmp_path):
    database = tmp_path / "agent.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE session_nodes(session_key TEXT PRIMARY KEY, current_session_id TEXT);
            CREATE TABLE trajectory_runtime_events(
                session_id TEXT, seq INTEGER, run_id TEXT, event_json TEXT, created_at INTEGER);
            INSERT INTO session_nodes VALUES('agent:main:workflow-example-script','session-1');
        """)

    def add(seq, kind="tool.call", *, session="session-1", key=KEY, call="call-1",
            name="read", args=None, success=None, run="run-1", timestamp=9999999999999):
        data = {"toolCallId": call, "name": name, "args": args or {}, "success": success,
                "result": "PRIVATE TOOL OUTPUT NEVER DISPLAY"}
        event = {"type": kind, "sessionKey": key, "runId": run, "data": data,
                 "thinking": "PRIVATE REASONING NEVER DISPLAY", "prompt": "PRIVATE PROMPT"}
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO trajectory_runtime_events VALUES(?,?,?,?,?)",
                               (session, seq, run, json.dumps(event), timestamp))

    return database, add


def test_actual_skill_and_artifact_names_without_commands_or_results(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    add(0, args={"path": str(tmp_path / "skills/openclaw/text-polisher/SKILL.md")})
    add(1, "tool.result", success=True)
    add(2, call="ref", args={"file_path": str(tmp_path / "skills/openclaw/text-polisher/references/checklist.md")})
    add(3, call="write", name="write", args={"path": str(tmp_path / "outputs/draft.md"), "content": "PRIVATE BODY"})
    add(4, "tool.result", call="write", name="write", success=True)
    add(5, call="exec", name="exec", args={"command": "dangerous PRIVATE COMMAND token=secret"})
    add(6, "tool.result", call="exec", name="exec", success=False)
    output = reader.poll()
    assert output == [
        "工具开始：read · Skill text-polisher / SKILL.md",
        "工具完成：read · Skill text-polisher / SKILL.md",
        "工具开始：read · Skill text-polisher / references / checklist.md",
        "工具开始：write · outputs/draft.md",
        "工具完成：write · outputs/draft.md",
        "工具开始：exec", "工具失败：exec"]
    assert not any("PRIVATE" in text or "secret" in text for text in output)
    assert reader.poll() == []


def test_checkpoint_other_sessions_and_private_events_are_excluded(trace, tmp_path):
    database, add = trace
    add(0, name="old_tool")
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    add(1, session="foreign-session", name="foreign_tool")
    add(1, key="agent:main:foreign", name="wrong_key_tool")
    add(2, "model.thinking", name="PRIVATE")
    add(3, "context.snapshot", name="PRIVATE")
    add(4, name="current_tool")
    assert reader.poll() == ["工具开始：current_tool"]


def test_duplicate_start_end_are_deduplicated_per_run(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    for seq, kind in enumerate(["tool.call", "tool.call", "tool.result", "tool.result"]):
        add(seq, kind, success=True)
    assert reader.poll() == ["工具开始：read", "工具完成：read"]
    add(4, run="run-2")
    assert reader.poll() == ["工具开始：read"]


def test_new_session_admission_and_rotation_do_not_replay_old_events(trace, tmp_path):
    database, add = trace
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM session_nodes")
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    assert reader.available and reader.poll() == []
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO session_nodes VALUES(?,?)", (KEY, "new-session"))
    add(0, session="new-session", timestamp=0, name="old_tool")
    add(1, session="new-session", name="new_tool")
    assert reader.poll() == ["工具开始：new_tool"]


def test_sensitive_and_outside_paths_are_not_exposed(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    add(0, call="secret", args={"path": str(tmp_path / ".env")})
    add(1, call="private", args={"path": str(tmp_path.parent / "private/draft.md")})
    add(2, call="traversal", args={"path": "../private.md"})
    add(3, call="badname", name="read PRIVATE\nTOKEN")
    assert reader.poll() == ["工具开始：read · 受保护配置文件", "工具开始：read · draft.md", "工具开始：read"]


def test_batch_limit_does_not_drop_events(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    for seq in range(205):
        add(seq, call=f"call-{seq}")
    assert len(reader.poll()) == 200
    assert len(reader.poll()) == 5
    assert reader.poll() == []


def test_unknown_result_status_is_not_reported_as_success(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    add(0, "tool.result")
    assert reader.poll() == ["工具返回（状态未标明）：read"]


def test_missing_and_old_schema_are_truthful_without_creating_state(tmp_path):
    database = tmp_path / "missing.sqlite"
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    assert not reader.available and not database.exists()
    events = []
    asyncio.run(reader.relay(lambda: True, events.append, interval=.001))
    assert events == ["当前网关未提供工具执行细节；可继续查看正文和最终结果。"]
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE old_schema(name TEXT)")
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    assert not reader.available and reader.poll() == []


def test_relay_drains_last_result_before_returning(trace, tmp_path):
    database, add = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    running = True
    events = []
    add(0)
    def emit(text):
        nonlocal running
        events.append(text)
        if text == "工具开始：read":
            add(1, "tool.result", success=True)
            running = False
    asyncio.run(reader.relay(lambda: running, emit, interval=.001))
    assert events == ["工具开始：read", "工具完成：read"]


def test_relay_can_be_cancelled_without_touching_trace(trace, tmp_path):
    database, _ = trace
    reader = OpenClawToolActivity(KEY, tmp_path, database)
    async def scenario():
        task = asyncio.create_task(reader.relay(lambda: True, lambda _: None, interval=10))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM trajectory_runtime_events").fetchone()[0] == 0
