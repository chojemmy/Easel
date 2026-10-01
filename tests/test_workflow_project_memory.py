import asyncio
import json

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, node_of
from easel.workflow_agent_context import current_checkpoint, project_memory
import pytest


def test_chat_uses_native_project_agent_and_keeps_feedback_across_nodes(tmp_path, monkeypatch):
    service = ContentWorkflowService(tmp_path, vault=tmp_path / "vault")
    project = service.create({"title": "语义字幕"})
    calls = []
    class Native:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("on_text"):
                kwargs["on_text"]("已沿用你的字幕要求。")
            return '已沿用你的字幕要求。\n<easel_action>{"action":"chat"}</easel_action>'
    service.skill_agent = Native()
    async def wrong_model(*args, **kwargs):
        raise AssertionError("Production chat must not start an independent model conversation")
    monkeypatch.setattr(chat.WorkflowModel, "generate", wrong_model)
    async def send(node, message):
        current = service.get(project["id"])
        await service.chat(project["id"], node, {"message": message, "content_version": current["content_version"]})
        await service.tasks[project["id"]]
    async def run():
        await send("brief", "本片每条字幕最多12字，末尾不要标点")
        service.feedback(project["id"], "script", "保留我的第一人称口语")
        await send("transcript", "接续前面的字幕要求")
    asyncio.run(run())
    assert len(calls) == 2 and calls[0]["project_id"] == calls[1]["project_id"]
    memory = calls[1]["context"]
    assert '"action":"chat|update|draft|run|execute_skill"' in calls[1]["system"]
    assert "read_skill 的 skills" not in calls[1]["system"], "Native Agent uses its real tools, not host pseudo tools"
    assert any("最多12字" in m["content"] for m in memory["project_conversation"])
    assert any("第一人称口语" in f["text"] for n in memory["nodes"] for f in n["feedback"])
    assert memory["project_state_path"].endswith("project.json")
    assert node_of(service.get(project["id"]), "transcript")["chat"]["messages"][-1]["status"] == "completed"
    other = service.create({"title": "其他项目"})
    assert "最多12字" not in json.dumps(project_memory(other), ensure_ascii=False)


def test_subtitle_length_edits_invalidate_subtitles_and_downstream_but_keep_recording(tmp_path):
    service = ContentWorkflowService(tmp_path, vault=tmp_path / "vault")
    p = service.create({"title": "调整字幕"})
    for name in ("source", "transcript", "storyboard", "build"):
        node_of(p, name)["status"] = "completed"
    service.save(p)
    updated = service.patch(p["id"], {"settings": {"subtitle_max_chars": 16}})
    assert node_of(updated, "source")["status"] == "completed"
    assert all(node_of(updated, name)["status"] == "stale" for name in ("transcript", "storyboard", "build"))
    assert updated["settings"]["subtitle_max_chars"] == 16
    for invalid in (7, 41, "12", 12.5, 16.0, True):
        with pytest.raises(ValueError, match="字幕字数"):
            service.patch(p["id"], {"settings": {"subtitle_max_chars": invalid}})


def test_current_checkpoint_keeps_selected_manuscript_distinct_from_stale_chat_names(tmp_path):
    service = ContentWorkflowService(tmp_path, vault=tmp_path / "vault")
    p = service.create({"title": "视频项目名"})
    p["manuscripts"] = [{"id": "ms-primary", "title": "新稿件", "content": "实际口播稿"}]
    p["primary_manuscript_id"] = "ms-primary"
    node_of(p, "transcript")["chat"] = {"messages": [{"role": "assistant", "content": "主稿叫旧错误名称", "status": "completed"}]}
    memory = project_memory(p)
    receipt = current_checkpoint(memory, p["id"])
    assert '"project_title": "视频项目名"' in receipt
    assert '"primary_manuscript_title": "新稿件"' in receipt
    assert "旧错误名称" not in receipt
    assert current_checkpoint({"project_memory": memory}, p["id"]) == receipt
    assert current_checkpoint(memory, "wf-111111111111") == ""


def test_native_request_sends_latest_facts_once_and_does_not_replay_failed_agent_output(tmp_path):
    service = ContentWorkflowService(tmp_path, vault=tmp_path / "vault")
    p = service.create({"title": "分镜项目"})
    p["manuscripts"] = [{"id": "selected", "title": "新稿件", "content": "最新主稿保留全文"}]
    p["primary_manuscript_id"] = "selected"
    node_of(p, "transcript")["chat"] = {"messages": [
        {"role": "user", "content": "每条字幕12字，末尾无标点", "status": "completed"},
        {"role": "assistant", "content": "失效内容" * 6000, "status": "failed"}]}
    node_of(p, "storyboard")["chat"] = {"messages": [{"role": "user", "content": "网页规划、本机执行，先讨论", "status": "completed"}]}
    memory = project_memory(p, tmp_path, compact=True)
    request = json.loads(chat.context_for(p, "storyboard", [], [{"name": "video-production", "description": "分镜方法",
        "source_path": "/skills/video-production/SKILL.md", "execution_note": "冗余描述" * 500}], native=True))
    assert request["request"] == "网页规划、本机执行，先讨论"
    assert not any(k in request for k in ("project_memory", "history", "manuscripts"))
    assert "失效内容" not in json.dumps(memory, ensure_ascii=False)
    assert memory["primary_manuscript"]["content"] == "最新主稿保留全文"
    assert any("12字" in m["content"] for m in memory["project_conversation"])
    assert "execution_note" not in request["available_skills"][0]


def test_length_retry_stays_native_and_never_applies_partial_action_or_repairs_format(tmp_path):
    from easel.workflow_skill_agent import WorkflowSkillAgentTruncated
    service = ContentWorkflowService(tmp_path, vault=tmp_path / "vault")
    p = service.create({"title": "重试分镜"})
    calls = []
    class Native:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                kwargs["on_text"]("The")
                raise WorkflowSkillAgentTruncated("length")
            kwargs["on_text"]("先讨论分镜。")
            return '先讨论分镜。\n<easel_action>{"action":"chat"}</easel_action>'
    service.skill_agent = Native()
    async def run():
        await service.chat(p["id"], "storyboard", {"message": "先讨论，不执行", "content_version": p["content_version"]})
        await service.tasks[p["id"]]
    asyncio.run(run())
    result = service.get(p["id"]); n = node_of(result, "storyboard")
    assert len(calls) == 2 and all(c["project_id"] == p["id"] for c in calls)
    assert calls[1]["generation_budget"] == "maximum"
    assert "修复操作格式" not in calls[1]["system"]
    assert n["chat"]["messages"][-1]["content"] == "先讨论分镜。"
    assert n["status"] == "idle" and not n["runs"] and result["content_version"] == p["content_version"]
