import asyncio
import json

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, node_of
from easel.workflow_agent_context import project_memory
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
