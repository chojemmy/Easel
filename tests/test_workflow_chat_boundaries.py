"""Node chat must own its turn and finish before it can mutate a project."""
import asyncio
import json
from pathlib import Path

import pytest

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, WorkflowConflict, node_of
from easel.workflow_runner import WorkflowRunner


@pytest.fixture
def service(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return ContentWorkflowService(tmp_path / "project", vault=vault)


async def send(service, project, node="script", client="request-1", message="请生成新稿"):
    return await service.chat(project["id"], node, {"message": message,
        "content_version": project["content_version"], "client_message_id": client})


def test_chat_draft_consumes_pending_feedback_without_generating_twice(service, monkeypatch):
    service.executor = WorkflowRunner(service.root)
    calls = []
    async def generate(self, *args, **kwargs):
        calls.append(args)
        return json.dumps({"reply": "根据意见写成的新稿", "action": "draft"}, ensure_ascii=False)
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "稿件保留", "manuscripts": [
        {"id": "original", "title": "原稿", "content": "用户原始内容", "source_kind": "manual"}]})
    service.feedback(project["id"], "script", "开头改成现场故事")
    async def run():
        await send(service, project)
        await service.tasks[project["id"]]
    asyncio.run(run())
    final = service.get(project["id"])
    assert len(calls) == 1, "Adopting the completed chat draft must not trigger another model request"
    assert len(final["manuscripts"]) == 2
    assert final["manuscripts"][0] == project["manuscripts"][0]
    assert final["manuscripts"][-1]["content"] == "根据意见写成的新稿"
    assert final["primary_manuscript_id"] == final["manuscripts"][-1]["id"]


def test_completed_duplicate_request_is_idempotent_and_conflicting_reuse_rejected(service, monkeypatch):
    calls = []
    async def generate(self, *args, **kwargs):
        calls.append(args)
        return json.dumps({"reply": "填写好了", "action": "update", "updates": {"title": "新标题"}})
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "原标题"})
    async def run():
        await send(service, project, "brief", message="修改标题")
        await service.tasks[project["id"]]
        duplicate = await send(service, project, "brief", message="修改标题")
        assert len(node_of(duplicate, "brief")["chat"]["messages"]) == 2
        with pytest.raises(WorkflowConflict):
            await send(service, duplicate, "brief", message="同一编号的另一条消息")
    asyncio.run(run())
    assert len(calls) == 1
    assert service.get(project["id"])["title"] == "新标题"


@pytest.mark.parametrize("raw",[
    '{"reply":"已写出的半段正文", "action":"draft"',
    '{"reply":"已想好新标题", "action":"update", "updates":{"title":"未完成标题"}',
])
def test_incomplete_result_keeps_partial_reply_but_never_applies_content(service, monkeypatch, raw):
    async def generate(self, *args, **kwargs):
        kwargs["on_text"](raw)
        return raw
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "原标题", "manuscripts": [{"id": "old", "content": "原稿"}]})
    async def run():
        await send(service, project)
        await service.tasks[project["id"]]
    asyncio.run(run())
    final = service.get(project["id"])
    assert final["title"] == project["title"]
    assert final["manuscripts"] == project["manuscripts"]
    assert final["content_version"] == project["content_version"]
    state = node_of(final, "script")["chat"]
    assert state["status"] == "failed"
    assert state["messages"][-1]["content"] == chat.partial_reply(raw)


@pytest.mark.parametrize("replacement_status",["stopped", "running"])
def test_late_completion_cannot_apply_after_turn_is_stopped_or_replaced(service, monkeypatch, replacement_status):
    async def generate(self, *args, **kwargs):
        current = service.get(project["id"])
        state = node_of(current, "brief")["chat"]
        state["status"] = replacement_status
        if replacement_status == "running":
            state["turn_id"] = "new-turn"
            state["messages"][-1].update(id="new-turn", content="新一轮正在生成", status="streaming")
        service.save(current)
        return json.dumps({"reply": "旧轮晚到", "action": "update", "updates": {"title": "不应覆盖"}})
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "保持原标题"})
    async def run():
        await send(service, project, "brief")
        await service.tasks[project["id"]]
    asyncio.run(run())
    final = service.get(project["id"])
    assert final["title"] == project["title"]
    assert final["content_version"] == project["content_version"]
    state = node_of(final, "brief")["chat"]
    assert state["status"] == replacement_status
    if replacement_status == "running":
        assert state["messages"][-1]["content"] == "新一轮正在生成"


def test_stop_still_prevents_patch_when_provider_returns_after_cancellation(service, monkeypatch):
    started = asyncio.Event()
    async def generate(self, *args, **kwargs):
        kwargs["on_text"]('{"reply":"已经生成的半句')
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A transport may finish its response while cancellation is in flight.
            return json.dumps({"reply": "迟来的完整稿", "action": "draft"})
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "停止后不落稿"})
    async def run():
        await send(service, project)
        await asyncio.wait_for(started.wait(), timeout=1)
        final = await service.stop(project["id"], "script")
        assert final["manuscripts"] == []
        assert final["content_version"] == project["content_version"]
        state = node_of(final, "script")["chat"]
        assert state["status"] == "stopped"
        assert state["messages"][-1]["content"] == "已经生成的半句"
        assert project["id"] not in service.tasks
    asyncio.run(run())


@pytest.mark.parametrize("node,updates",[
    ("script", {"primary_manuscript_id": "other"}),
    ("build", {"settings": {"visual_style": "红色", "publish_platform": "douyin"}}),
    ("brief", {"brief": {"topic": "合法值"}, "nodes": [{"id": "publish", "status": "completed"}]}),
])
def test_cross_node_or_control_field_updates_fail_atomically(service, monkeypatch, node, updates):
    async def generate(self, *args, **kwargs):
        return json.dumps({"reply": "越权建议", "action": "update", "updates": updates})
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "边界"})
    async def run():
        await send(service, project, node)
        await service.tasks[project["id"]]
    asyncio.run(run())
    final = service.get(project["id"])
    for field in ("title", "brief", "settings", "media", "manuscripts", "primary_manuscript_id", "content_version"):
        assert final[field] == project[field]
    assert node_of(final, node)["chat"]["status"] == "failed"
    assert node_of(final, "publish")["status"] == "idle"


def test_retrieved_notes_outside_vault_or_private_directories_are_not_in_model_context(service, monkeypatch):
    (service.vault / "正常资料.md").write_text("可用的正文", encoding="utf-8")
    forbidden = [service.vault.parent / "外部资料.md", service.vault / "_Agent/私密资料.md", service.vault / ".hidden/隐藏资料.md"]
    for path in forbidden:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_NOTE_MUST_NOT_REACH_MODEL", encoding="utf-8")
    monkeypatch.setattr(service, "obsidian_search", lambda _: [
        {"title": path.stem, "path": str(path.relative_to(service.vault)) if path.is_relative_to(service.vault) else "../外部资料.md"}
        for path in [*forbidden, service.vault / "正常资料.md"]])
    prompts = []
    async def generate(self, prompt, **kwargs):
        prompts.append(json.loads(prompt))
        if len(prompts) == 1:
            return json.dumps({"reply": "查找资料", "action": "search", "query": "资料"})
        return json.dumps({"reply": "已依据资料回答", "action": "chat"})
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    project = service.create({"title": "检索安全"})
    async def run():
        await send(service, project)
        await service.tasks[project["id"]]
    asyncio.run(run())
    assert len(prompts) == 2
    assert [note["content"] for note in prompts[-1]["retrieved_notes"]] == ["可用的正文"]
    assert "PRIVATE_NOTE_MUST_NOT_REACH_MODEL" not in json.dumps(prompts)


@pytest.mark.parametrize("target_kind",["outside", "private"])
def test_obsidian_search_does_not_read_symlinks_to_outside_or_private_notes(service, monkeypatch, target_kind):
    target = service.vault.parent / "outside.md" if target_kind == "outside" else service.vault / "_Agent/private.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("PRIVATE_NOTE_MUST_NOT_BE_READ", encoding="utf-8")
    link = service.vault / "公开资料.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    original_read = Path.read_text
    def guarded_read(path, *args, **kwargs):
        if path.resolve() == target.resolve():
            pytest.fail("Search must reject the resolved private/outside path before reading an excerpt")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert service.obsidian_search("资料") == []
