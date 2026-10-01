import asyncio
import json

from easel import workflow_chat as chat
from easel.content_workflow import ContentWorkflowService, node_of
from easel.workflow_agent_context import project_memory


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
    assert any("最多12字" in m["content"] for m in memory["project_conversation"])
    assert any("第一人称口语" in f["text"] for n in memory["nodes"] for f in n["feedback"])
    assert memory["project_state_path"].endswith("project.json")
    assert node_of(service.get(project["id"]), "transcript")["chat"]["messages"][-1]["status"] == "completed"
    other = service.create({"title": "其他项目"})
    assert "最多12字" not in json.dumps(project_memory(other), ensure_ascii=False)
