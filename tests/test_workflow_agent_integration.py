"""Original chat bridge boundaries, without importing production web.app."""
import ast
import asyncio
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, node_of


def bridge_namespace(include_stop=False):
    source = Path(__file__).resolve().parents[1] / "web/app.py"
    module = ast.parse(source.read_text(encoding="utf-8-sig"))
    selected = []
    for node in module.body:
        functions = {"_workflow_agent_start", "_workflow_agent_stop"} | ({"api_chat_stop"} if include_stop else set())
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in functions:
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(target, ast.Name) and target.id.startswith(("_WORKFLOW_", "_RUNNING_CHAT")) for target in targets):
                selected.append(node)
    namespace = {"ChatRequest": SimpleNamespace, "StopRequest": SimpleNamespace, "asyncio": asyncio,
                 "subprocess": subprocess, "time": time, "_STOPPED_CHAT": set()}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def gateway_http_proc_class():
    source = Path(__file__).resolve().parents[1] / "web/app.py"
    module = ast.parse(source.read_text(encoding="utf-8-sig"))
    selected = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "_GatewayHttpProc"]
    namespace = {"subprocess": subprocess, "time": time}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_GatewayHttpProc"]


def test_http_stop_waits_for_transport_cleanup_before_reporting_done():
    async def run():
        proc = gateway_http_proc_class()()
        cleanup_started, allow_cleanup = asyncio.Event(), asyncio.Event()
        async def transport():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                proc.finish()
        proc._task = asyncio.create_task(transport())
        await asyncio.sleep(0)
        proc.terminate()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert proc.poll() is None
        # Supervisor and stop API may both terminate; do not cancel cleanup twice.
        proc.terminate()
        with pytest.raises(subprocess.TimeoutExpired):
            await asyncio.to_thread(proc.wait, timeout=.01)
        assert not proc._task.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await proc._task
        assert proc.poll() == 0
        assert await asyncio.to_thread(proc.wait, timeout=.01) == 0
    asyncio.run(run())


def test_http_stop_before_transport_starts_still_finishes():
    async def run():
        proc = gateway_http_proc_class()()
        async def transport():
            pytest.fail("A cancelled queued transport must not start")
        proc._task = asyncio.create_task(transport())
        proc.terminate()
        assert proc.poll() is None
        with pytest.raises(asyncio.CancelledError):
            await proc._task
        assert await asyncio.to_thread(proc.wait, timeout=1) == 0
    asyncio.run(run())


def test_stop_before_supervisor_claims_turn_still_marks_new_turn_for_cancellation():
    bridge = bridge_namespace()
    old_snapshot = {"turn_id": "previous-turn", "status": "done"}
    called = []
    async def start(req):
        # Production creates a supervisor task before its first _save_turn runs.
        return SimpleNamespace(body_iterator=None)
    async def last(session_id):
        return old_snapshot
    async def stop(req):
        called.append(req.sessionId)
        return {"stopped": False}
    bridge.update(api_chat_stream=start, api_chat_last=last, api_chat_stop=stop)
    async def run():
        await bridge["_workflow_agent_start"](message="任务", session_id="workflow-node", turn_id="new-turn")
        await bridge["_workflow_agent_stop"](session_id="workflow-node", turn_id="new-turn")
    asyncio.run(run())
    assert ("workflow-node", "new-turn") in bridge["_WORKFLOW_STOP_REQUESTS"], "Cancellation cannot depend on a supervisor having persisted its first snapshot"


def test_old_stop_does_not_terminate_newer_original_chat_turn():
    bridge = bridge_namespace()
    called = []
    async def last(session_id):
        return {"turn_id": "new-turn", "status": "running"}
    async def stop(req):
        called.append(req.sessionId)
        return {"stopped": True}
    bridge.update(api_chat_last=last, api_chat_stop=stop)
    bridge["_WORKFLOW_AGENT_TURNS"]["workflow-node"] = "new-turn"
    result = asyncio.run(bridge["_workflow_agent_stop"](session_id="workflow-node", turn_id="old-turn"))
    assert result == {"stopped": False} and called == []


def test_queued_turn_cancellation_does_not_kill_the_previous_turn_process():
    bridge = bridge_namespace(include_stop=True)
    class Proc:
        def poll(self):
            return None
        def terminate(self):
            pytest.fail("Cancelling a queued turn must not terminate the preceding process")
    bridge["_WORKFLOW_AGENT_TURNS"]["workflow-node"] = "queued-turn"
    bridge["_RUNNING_CHAT_TURNS"]["workflow-node"] = "previous-turn"
    bridge["_RUNNING_CHAT"]["workflow-node"] = Proc()
    async def last(session_id):
        # The queued supervisor already claimed its snapshot before getting the lock.
        return {"turn_id": "queued-turn", "status": "running"}
    bridge["api_chat_last"] = last
    result = asyncio.run(bridge["_workflow_agent_stop"](session_id="workflow-node", turn_id="queued-turn"))
    assert result == {"stopped": False}
    assert ("workflow-node", "queued-turn") in bridge["_WORKFLOW_STOP_REQUESTS"]


def test_matching_turn_cancellation_terminates_only_that_registered_process():
    bridge = bridge_namespace(include_stop=True)
    terminated = []
    class Proc:
        def poll(self):
            return None
        def terminate(self):
            terminated.append(True)
            bridge["_RUNNING_CHAT"].pop("workflow-node")
        def wait(self, timeout):
            return 0
    bridge["_WORKFLOW_AGENT_TURNS"]["workflow-node"] = "current-turn"
    bridge["_RUNNING_CHAT_TURNS"]["workflow-node"] = "current-turn"
    bridge["_RUNNING_CHAT"]["workflow-node"] = Proc()
    async def last(session_id):
        return {"turn_id": "current-turn", "status": "done"}
    bridge["api_chat_last"] = last
    result = asyncio.run(bridge["_workflow_agent_stop"](session_id="workflow-node", turn_id="current-turn"))
    assert result == {"stopped": True} and terminated == [True]
    assert ("workflow-node", "current-turn") not in bridge["_WORKFLOW_STOP_REQUESTS"]


class Catalog:
    def __init__(self, root):
        pass
    def list_skills(self, **kwargs):
        return [{"name": "text-polisher", "id": "text-polisher", "layer": "produce"}]
    def read_skill(self, name, **kwargs):
        return {"name": name, "id": name, "path": "SKILL.md", "sha256": "test-hash", "content": "原文润色规范"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(chat, "WorkflowSkillCatalog", Catalog)
    return ContentWorkflowService(tmp_path / "root", vault=vault)


@pytest.mark.parametrize("replacement",["stopped", "new-turn", "new-version"])
def test_late_model_action_cannot_start_full_agent_after_stop_or_replacement(service, monkeypatch, replacement):
    calls = []
    class Delegate:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            return {"text": "不应执行的真实工具"}
    service.skill_agent = Delegate()
    project = service.create({"title": "取消边界"})
    async def generate(self, *args, **kwargs):
        current = service.get(project["id"])
        owner = node_of(current, "script")["chat"]
        if replacement == "stopped":
            owner["status"] = "stopped"
        elif replacement == "new-version":
            current["content_version"] += 1
        else:
            owner["turn_id"] = replacement
        service.save(current)
        return "准备执行技能\n<easel_action>" + json.dumps({"action": "execute_skill", "skill_name": "text-polisher", "task": "润色当前稿件"}) + "</easel_action>"
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    async def run():
        await service.chat(project["id"], "script", {"message": "润色", "content_version": project["content_version"], "client_message_id": "once"})
        await service.tasks[project["id"]]
    asyncio.run(run())
    assert calls == [], "A stale/finished model turn cannot launch side-effecting tools"
    assert not list(service.directory(project["id"]).glob("artifacts/agent-*"))


def test_delegated_artifacts_are_real_non_symlink_files_under_own_run_directory(service, monkeypatch, tmp_path):
    calls = []
    outside = tmp_path / "outside.md"
    outside.write_text("外部正文", encoding="utf-8")
    class Delegate:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            directory = kwargs["directory"]
            (directory / "成稿.md").write_text("实际生成的稿件", encoding="utf-8")
            (directory / ".internal.md").write_text("内部信息", encoding="utf-8")
            try:
                (directory / "外部链接.md").symlink_to(outside)
            except OSError:
                pass
            kwargs["on_event"]({"kind": "tool", "text": "text-polisher：保存实际稿件"})
            return {"text": "输出一份当前项目稿件"}
    service.skill_agent = Delegate()
    count = 0
    async def generate(self, *args, **kwargs):
        nonlocal count
        count += 1
        action = {"action": "execute_skill", "skill_name": "text-polisher", "task": "润色当前稿件"} if count == 1 else {"action": "chat"}
        return "结果\n<easel_action>" + json.dumps(action) + "</easel_action>"
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "真实产物"})
    async def run():
        await service.chat(project["id"], "script", {"message": "调用原技能", "content_version": project["content_version"], "client_message_id": "once"})
        await service.tasks[project["id"]]
    asyncio.run(run())
    final = node_of(service.get(project["id"]), "script")
    assert len(calls) == 1 and count == 2
    assert [artifact["name"] for artifact in final["artifacts"]] == ["成稿.md"]
    assert Path(final["artifacts"][0]["path"]).is_relative_to(calls[0]["directory"])
    assert any("保存实际稿件" in activity["text"] for activity in final["activity"])
