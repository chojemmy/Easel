"""Exercise the original-chat bridge without importing web.app or running a model."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from easel.workflow_skill_agent import WorkflowSkillAgent, WorkflowSkillAgentError, WorkflowSkillAgentNeedsInput


@pytest.fixture
def case(tmp_path):
    pid = "wf-0123456789ab"
    directory = tmp_path / "outputs/视频工作流" / pid / "artifacts/agent-script-turn1"
    directory.mkdir(parents=True)
    return {"project_id": pid, "node": "script", "skill": {"name": "text-polisher", "layer": "produce"},
            "instruction": "按原技能给当前稿件去 AI 味，保留事实。", "directory": directory,
            "context": {"primary_manuscript": "原始稿件只读"}}, tmp_path


def event(kind, data):
    # Exactly the public dict shape yielded by web.app.api_chat_stream.forward.
    return {"id": "1", "event": kind, "data": json.dumps(data, ensure_ascii=False)}


def test_real_event_source_iterator_exposes_public_text_and_activity_only(case, monkeypatch):
    args, root = case
    events, starts, stops = [], [], []
    monkeypatch.setenv("TEST_SKILL_KEY", "credential-must-not-display")
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            yield {"event": "thinking", "data": "INTERNAL REASONING NOT EVEN JSON"}
            yield event("activity", "读取 text-polisher/SKILL.md")
            yield event("token", "改后稿")
            yield event("token", "正文 credential-must-not-display")
            yield event("done", {"sessionKey": kwargs["session_id"]})
        return SimpleNamespace(body_iterator=body())
    async def stop(**kwargs):
        stops.append(kwargs)
    result = asyncio.run(WorkflowSkillAgent(start, stop, project_root=root).execute(**args, on_event=events.append))
    assert result["text"] == "改后稿正文 [已隐藏凭证]"
    assert result["skill"] == "text-polisher" and not stops
    assert str(args["directory"]) in starts[0]["message"]
    assert "实际执行 /text-polisher" in starts[0]["message"] and "只读" in starts[0]["message"]
    assert "INTERNAL REASONING" not in json.dumps(events)
    assert all(item["kind"] in {"status", "tool", "generation"} for item in events)
    assert [item["text"] for item in events if item["kind"] == "generation"][-1] == result["text"]
    assert any(item["kind"] == "tool" and "读取 text-polisher/SKILL.md" in item["text"] for item in events)
    assert not list(args["directory"].iterdir()), "Adapter must not turn model prose into unvalidated files"


def test_sessions_span_nodes_with_unique_turns_and_isolate_projects(case):
    args, root = case
    starts = []
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            yield event("token", "正文")
            yield event("done", {"sessionKey": kwargs["session_id"]})
        return body()
    async def stop(**kwargs):
        pytest.fail("Completed turn must not stop another turn")
    async def run():
        agent = WorkflowSkillAgent(start, stop, project_root=root)
        first = await agent.execute(**args)
        second = await agent.execute(**args)
        third = await agent.execute(**{**args, "node": "brief"})
        assert first["session_key"] == second["session_key"]
        assert first["session_key"] == third["session_key"] == "workflow-wf-0123456789ab"
        other = {**args, "project_id": "wf-abcdef012345", "directory": root / "outputs/视频工作流/wf-abcdef012345/artifacts/turn"}
        fourth = await agent.execute(**other)
        assert fourth["session_key"] != first["session_key"]
        assert len({first["turn_id"], second["turn_id"], third["turn_id"]}) == 3
    asyncio.run(run())


def test_chat_generation_and_skill_execution_share_native_session(case):
    args, root = case
    starts, text = [], []
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            yield event("token", "实际正文")
            yield event("done", {"sessionKey": kwargs["session_id"]})
        return body()
    async def stop(**kwargs):
        pytest.fail("Completed conversation must not cancel")
    async def run():
        agent = WorkflowSkillAgent(start, stop, project_root=root)
        await agent.generate(project_id=args["project_id"], node="brief", prompt="本项目每条字幕12字",
            system="只输出正文", context={"previous_feedback": "句尾无标点"}, on_text=text.append)
        await agent.execute(**args)
    asyncio.run(run())
    assert text == ["实际正文"]
    assert starts[0]["session_id"] == starts[1]["session_id"]
    assert "句尾无标点" in starts[0]["message"] and "统一 Agent 会话" in starts[0]["message"]


def test_pins_current_library_source_instead_of_profile_copy(case):
    args, root = case
    source = root / "skills/openclaw/text-polisher/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("Current learned skill", encoding="utf-8")
    args["skill"]["source_path"] = str(source)
    starts = []
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            yield event("token", "完成")
            yield event("done", {"sessionKey": kwargs["session_id"]})
        return body()
    async def stop(**kwargs):
        pytest.fail("Completed source-pinned run must not stop")
    asyncio.run(WorkflowSkillAgent(start, stop, project_root=root).execute(**args))
    assert str(source.resolve()) in starts[0]["message"]
    assert "references 从它的父目录解析" in starts[0]["message"]


@pytest.mark.parametrize("failure",["missing_done", "wrong_session", "malformed", "error"])
def test_partial_or_failed_skill_result_cannot_be_successful_and_stops_its_own_turn(case, failure):
    args, root = case
    starts, stops = [], []
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            yield event("token", "未完成的正文")
            if failure == "wrong_session":
                yield event("done", {"sessionKey": "some-other-project"})
            elif failure == "malformed":
                yield {"event": "token", "data": '"unfinished'}
            elif failure == "error":
                yield event("error", "工具失败 api_key=private-value")
        return body()
    async def stop(**kwargs):
        stops.append(kwargs)
    with pytest.raises(WorkflowSkillAgentError) as error:
        asyncio.run(WorkflowSkillAgent(start, stop, project_root=root).execute(**args))
    assert "private-value" not in str(error.value)
    assert stops == [{"session_id": starts[0]["session_id"], "turn_id": starts[0]["turn_id"]}]


def test_stop_propagates_to_original_agent_even_after_partial_stream(case):
    args, root = case
    entered = asyncio.Event()
    starts, stops, events, closed = [], [], [], []
    async def start(**kwargs):
        starts.append(kwargs)
        async def body():
            try:
                yield event("token", "半句正文")
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.append(True)
        return body()
    async def stop(**kwargs):
        stops.append(kwargs)
    async def run():
        task = asyncio.create_task(WorkflowSkillAgent(start, stop, project_root=root).execute(**args, on_event=events.append))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert stops == [{"session_id": starts[0]["session_id"], "turn_id": starts[0]["turn_id"]}]
    assert closed == [True]
    assert {"kind": "generation", "text": "半句正文"} in events


def test_per_run_timeout_calls_original_stop(case):
    args, root = case
    stops = []
    async def start(**kwargs):
        async def body():
            await asyncio.Event().wait()
            yield
        return body()
    async def stop(**kwargs):
        stops.append(kwargs)
    with pytest.raises(WorkflowSkillAgentError, match="超时"):
        asyncio.run(WorkflowSkillAgent(start, stop, project_root=root).execute(**args, timeout=.02))
    assert len(stops) == 1


def test_question_is_reported_and_original_agent_stopped_not_left_waiting(case):
    args, root = case
    stops = []
    async def start(**kwargs):
        async def body():
            yield event("question", {"questions": [{"question": "面向哪个受众？"}]})
        return body()
    async def stop(**kwargs):
        stops.append(kwargs)
    with pytest.raises(WorkflowSkillAgentNeedsInput) as error:
        asyncio.run(WorkflowSkillAgent(start, stop, project_root=root).execute(**args))
    assert "哪个受众" in error.value.question and len(stops) == 1


@pytest.mark.parametrize("change",[
    {"node": "publish"}, {"node": "archive"}, {"skill": {"name": "other", "layer": "publish"}},
    {"skill": {"name": "skill-publish-video", "layer": "produce"}}, {"skill": {"name": "../shell", "layer": "produce"}},
])
def test_publication_archive_and_invalid_skill_are_not_delegated(case, change):
    args, root = case
    async def unexpected(**kwargs):
        pytest.fail("This action must not reach the full agent")
    with pytest.raises(ValueError):
        asyncio.run(WorkflowSkillAgent(unexpected, unexpected, project_root=root).execute(**{**args, **change}))


def test_artifacts_are_limited_to_current_project_run_directory(case, tmp_path):
    args, root = case
    async def unexpected(**kwargs):
        pytest.fail("Out-of-scope directory must fail before starting agent")
    with pytest.raises(ValueError, match="目录"):
        asyncio.run(WorkflowSkillAgent(unexpected, unexpected, project_root=root).execute(**{**args, "directory": tmp_path / "unrelated"}))
