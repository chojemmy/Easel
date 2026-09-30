import asyncio
import json

import pytest

from easel.content_workflow import ContentWorkflowService, WorkflowConflict, node_of
from easel import workflow_chat as chat


@pytest.fixture
def service(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return ContentWorkflowService(tmp_path / "project", vault=vault)


class Adopt:
    async def execute(self, project, node, options, skill, directory, progress):
        progress({"kind": "tool", "text": "保存当前主稿"})
        return {"status": "awaiting_review", "message": "新稿已保存", "artifacts": []}


def model(monkeypatch, payload):
    async def generate(self, prompt, system="", on_text=None, **options):
        text = json.dumps(payload, ensure_ascii=False)
        if on_text:
            on_text(text[:18]); await asyncio.sleep(.2); on_text(text[18:])
        assert options["generation_budget"] in {"standard", "large", "maximum"}
        return text
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)


async def exchange(service, pid, node, message="帮我填写", client_id="first"):
    p = service.get(pid)
    started = await service.chat(pid, node, {"message": message, "content_version": p["content_version"], "client_message_id": client_id})
    task = service.tasks[pid]
    await task
    return started, service.get(pid)


def test_streaming_brief_updates_fields_and_keeps_node_history(service, monkeypatch):
    model(monkeypatch, {"reply": "建议面向施工企业，讲清楚实际案例。", "action": "update", "updates": {"brief": {"topic": "施工企业AI", "audience": "工程师"}}})
    p = service.create({"title": "初始主题"})
    async def run():
        started, final = await exchange(service, p["id"], "brief")
        assert node_of(started, "brief")["chat"]["status"] == "running"
        assert final["brief"]["topic"] == "施工企业AI"
        assert final["brief"]["platform"] == "视频号"
        assert len(node_of(final, "brief")["chat"]["messages"]) == 2
        assert node_of(final, "brief")["chat"]["messages"][-1]["status"] == "completed"
        assert len(node_of(final, "brief")["activity"]) >= 2
    asyncio.run(run())


def test_new_draft_is_main_without_overwriting_old_draft(service, monkeypatch):
    service.executor = Adopt()
    model(monkeypatch, {"reply": "# 新稿\n\n先说一个现场故事。", "action": "draft", "manuscript_title": "第二稿"})
    p = service.create({"title": "写稿", "manuscripts": [{"id": "old", "title": "原稿", "content": "原文", "source_kind": "manual"}]})
    async def run():
        _, final = await exchange(service, p["id"], "script", "改成故事开头")
        assert final["manuscripts"][0]["content"] == "原文"
        assert final["manuscripts"][-1]["content"].startswith("# 新稿")
        assert final["primary_manuscript_id"] != "old"
        assert node_of(final, "script")["status"] == "awaiting_review"
    asyncio.run(run())


def test_scope_violation_does_not_apply_any_updates(service, monkeypatch):
    model(monkeypatch, {"reply": "想写入其他字段", "action": "update", "updates": {"brief": {"topic": "changed"}, "settings": {"publish_platform": "douyin"}}})
    p = service.create({"title": "范围测试"})
    async def run():
        _, final = await exchange(service, p["id"], "brief")
        assert final["brief"] == p["brief"]
        assert node_of(final, "brief")["chat"]["status"] == "failed"
    asyncio.run(run())


def test_concurrent_form_edits_are_blocked_stop_is_durable(service, monkeypatch):
    async def generate(self, *args, **kwargs):
        kwargs["on_text"]('{"reply":"正在写第一句话')
        await asyncio.sleep(100)
    monkeypatch.setattr(chat.WorkflowModel, "generate", generate)
    p = service.create({"title": "取消测试"})
    async def run():
        body = {"message": "写一份稿", "content_version": p["content_version"], "client_message_id": "same"}
        await service.chat(p["id"], "script", body)
        duplicate = await service.chat(p["id"], "script", body)
        assert len(node_of(duplicate, "script")["chat"]["messages"]) == 2
        with pytest.raises(WorkflowConflict):
            service.patch(p["id"], {"title": "不能覆盖"})
        await asyncio.sleep(.01)
        stopped = await service.stop(p["id"], "script")
        assert node_of(stopped, "script")["chat"]["status"] == "stopped"
        assert stopped["manuscripts"] == []
        assert "第一句话" in node_of(stopped, "script")["chat"]["messages"][-1]["content"]
    asyncio.run(run())
    restored = ContentWorkflowService(service.root, service.vault).get(p["id"])
    assert node_of(restored, "script")["chat"]["status"] == "stopped"


def test_archive_chat_cannot_write_vault(service, monkeypatch):
    model(monkeypatch, {"reply": "准备好了，请查看归档预览。", "action": "run"})
    p = service.create({"title": "不能自动归档"})
    asyncio.run(exchange(service, p["id"], "archive"))
    assert not (service.vault / "3-输出").exists()


def test_budget_change_preserves_approved_outputs(service):
    p = service.create({"title": "预算"})
    for n in p["nodes"]:
        n.update(status="completed", approved_version=1)
    service.save(p)
    final = service.patch(p["id"], {"settings": {"generation_budget": "maximum"}})
    assert all(n["status"] == "completed" for n in final["nodes"])
    assert final["content_version"] == p["content_version"]  # Execution preference is not a new publishable version.


def test_partial_reply_decodes_unicode_and_does_not_expose_actions():
    assert chat.partial_reply('{"reply":"你好\\n继续\\u4e2d') == "你好\n继续中"
    assert chat.partial_reply('{"reply":"正文", "updates":{"secret":"hidden"}}') == "正文"
    assert chat.partial_reply('{"reply":"正文\\uD83D') == "正文"
    assert chat.partial_reply('{"reply":"正文\\uD83D\\uDE00"}') == "正文😀"


def test_old_chat_completion_cannot_close_new_turn(service):
    p = service.create({"title": "race"})
    node_of(p, "brief")["chat"] = {"status": "running", "turn_id": "new", "messages": []}
    service.save(p)
    chat.finish_chat(service, p["id"], "brief", "completed", turn_id="old")
    assert node_of(service.get(p["id"]), "brief")["chat"]["status"] == "running"
