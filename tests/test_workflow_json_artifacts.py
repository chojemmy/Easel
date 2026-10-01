"""Actual node artifacts remain validated even when native chat adds framing."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from easel.workflow_runner import RunnerBlocked, WorkflowRunner, parse_json_reply


@pytest.mark.parametrize("framing", [
    '{body}',
    '```json\n{body}\n```',
    '```json {body}```',
    '说明文字\n```json\n{body}\n```\n<easel_action>{{"action":"run"}}</easel_action>',
    '分镜结果：{body}\n结果说明',
])
def test_complete_artifact_is_extracted_without_executing_chat_metadata(framing):
    proposal = {"scenes": [{"title": "开场", "start": 0, "end": 10,
                            "purpose": "引出主题", "card": "网页规划"}]}
    assert parse_json_reply(framing.format(body=json.dumps(proposal, ensure_ascii=False))) == proposal


@pytest.mark.parametrize("reply", [
    '{"scenes":[]}\n{"scenes":[]}',
    '{"scenes":[]}{"scenes":[]}',
    '```json\n{"scenes":[]}\n```\n```json\n{"scenes":[]}\n```',
    '{"scenes":[\n{"title":"不可挽救嵌套片段"}',
    '```json\n{"scenes":[]}',
    '```json\n{"scenes":[]}\n```\n```json\n{"scenes":[]}',
    '```json\n{"scenes":[}\n```\n```json\n{"scenes":[]}\n```',
    '<easel_action>{"action":"run"}</easel_action>',
])
def test_incomplete_or_ambiguous_replies_cannot_become_artifacts(reply):
    with pytest.raises(RunnerBlocked):
        parse_json_reply(reply)


def test_valid_json_strings_are_not_altered_by_action_or_fence_detection():
    value = {"card": "引用 <easel_action> 不是操作指令", "purpose": "``` 保留原文"}
    assert parse_json_reply(json.dumps(value, ensure_ascii=False)) == value


@pytest.fixture
def storyboard_case(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    directory = root / "outputs/视频工作流/wf-0123456789ab"
    directory.mkdir(parents=True)
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(root / "isolated-openclaw"))
    monkeypatch.delenv("EASEL_OPENCLAW_WORKSPACE", raising=False)
    monkeypatch.delenv("EASEL_ROOT", raising=False)
    source_hash = "same-original-source"
    source = {"duration": 40, "source_sha256": source_hash}
    transcript = {"source_sha256": source_hash, "segments": [
        {"start": 0, "end": 7, "text": "网页规划"},
        {"start": 7, "end": 40, "text": "本机执行"},
    ]}
    for name, value in [("source-metadata.json", source), ("transcript.json", transcript)]:
        path = directory / "artifacts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    skill = root / "skills/openclaw/video-production"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: video-production\n---\n"
        "分镜读取 `references/workflow-storyboard.md`。\n转录读取 `references/workflow-subtitles.md`。", encoding="utf-8")
    (skill / "references/workflow-storyboard.md").write_text("本轮分镜专用规则：卡片间隔8秒", encoding="utf-8")
    (skill / "references/workflow-subtitles.md").write_text("这里是另一个节点的转录规则", encoding="utf-8")
    project = {"id": "wf-0123456789ab", "kind": "video", "title": "省钱方法", "content_version": 8,
               "manuscripts": [{"id": "m1", "content": "原始参考稿", "version": 1}],
               "primary_manuscript_id": "m1", "settings": {"generation_budget": "maximum"},
               "media": {}, "nodes": []}
    return root, directory, project


def timeline(second_start=10):
    return {"scenes": [
        {"start": 0, "end": second_start, "title": "规划", "purpose": "建立主题", "card": "网页规划"},
        {"start": second_start, "end": 40, "title": "执行", "purpose": "解释协作", "card": "本机执行"},
    ]}


def run_storyboard(case, agent, **options):
    root, directory, project = case
    events = []
    result = asyncio.run(WorkflowRunner(root, skill_agent=agent).execute(
        project, "storyboard", options, {"content": "节点规则"}, directory, events.append))
    return result, events


def test_storyboard_repairs_card_gap_in_same_agent_and_keeps_real_receipts(storyboard_case):
    calls = []
    class Agent:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["output_format"] == "json"
            assert kwargs["generation_budget"] == "maximum"
            assert "本轮分镜专用规则" in kwargs["system"]
            assert "这里是另一个节点的转录规则" not in kwargs["system"]
            if len(calls) == 1:
                return '说明\n```json\n' + json.dumps(timeline(7), ensure_ascii=False) + '\n```\n<easel_action>{"action":"chat"}</easel_action>'
            assert "相隔 7 秒" in kwargs["prompt"] and "尚未保存为有效结果" in kwargs["prompt"]
            return json.dumps(timeline(), ensure_ascii=False)
    root, directory, project = storyboard_case
    original_project = copy.deepcopy(project)
    files = {name: (directory / "artifacts" / name).read_bytes()
             for name in ("source-metadata.json", "transcript.json")}
    result, events = run_storyboard(storyboard_case, Agent())
    assert result["status"] == "awaiting_review" and len(calls) == 2
    assert {call["project_id"] for call in calls} == {project["id"]}
    assert project == original_project
    assert all((directory / "artifacts" / name).read_bytes() == data for name, data in files.items())
    saved = json.loads((directory / "artifacts/timeline.json").read_text(encoding="utf-8"))
    assert saved["source_sha256"] == "same-original-source"
    assert saved["timeline_mode"] == "original_no_cuts" and saved["scenes"] == timeline()["scenes"]
    assert any(event.get("phase") == "validating" and "相隔 7 秒" in event["text"]
               for event in events if isinstance(event, dict))
    reads = {(item["name"], item["path"]) for item in result["library_skills_used"]}
    assert ("video-production", "references/workflow-storyboard.md") in reads
    assert ("video-production", "references/workflow-subtitles.md") not in reads
    receipts = [Path(a["path"]) for a in result["artifacts"] if "Agent 产物回执" in a["name"]]
    assert len(receipts) == 2 and all(path.is_file() for path in receipts)
    assert "<easel_action>" in receipts[0].read_text(encoding="utf-8")
    preview = (directory / "artifacts/storyboard-preview.md").read_text(encoding="utf-8")
    assert "2 个章节，2 张卡片" in preview and "网页规划" in preview and "本机执行" in preview


def test_storyboard_format_failure_is_bounded_and_does_not_create_valid_timeline(storyboard_case):
    calls = []
    class Agent:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            return '{"scenes":['
    result, events = run_storyboard(storyboard_case, Agent())
    assert result["status"] == "blocked" and "连续 3 次" in result["message"] and len(calls) == 3
    assert not (storyboard_case[1] / "artifacts/timeline.json").exists()
    assert len([a for a in result["artifacts"] if "Agent 产物回执" in a["name"]]) == 3
    assert len([e for e in events if isinstance(e, dict) and "正在修正结果" in e["text"]]) == 2
    assert "修订本节点 Skill" not in result["message"]


def test_provider_failure_is_not_retried_as_a_json_format_error(storyboard_case):
    calls = []
    class Agent:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("模型接口 HTTP 529")
    result, _ = run_storyboard(storyboard_case, Agent())
    assert result["status"] == "failed" and "HTTP 529" in result["message"] and len(calls) == 1
    assert not (storyboard_case[1] / "artifacts/timeline.json").exists()


def test_invalid_imported_storyboard_is_not_silently_rewritten(storyboard_case):
    class Agent:
        async def generate(self, **kwargs):
            pytest.fail("User's imported design must be checked without model rewriting")
    result, _ = run_storyboard(storyboard_case, Agent(), timeline=timeline(7))
    assert result["status"] == "blocked" and "相隔 7 秒" in result["message"]
    assert not (storyboard_case[1] / "artifacts/timeline.json").exists()
