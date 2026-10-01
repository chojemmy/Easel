import asyncio
import copy
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from easel import workflow_runner as runner
from easel.workflow_asr import find_local_model


def model_at(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (path / name).write_text("fixture", encoding="utf-8")
    return path


def test_discovers_complete_hub_cache_prefers_small_over_tiny(tmp_path):
    hub = tmp_path / ".cache/huggingface/hub"
    model_at(hub / "models--Systran--faster-whisper-tiny/snapshots/aaa")
    small = model_at(hub / "models--Systran--faster-whisper-small/snapshots/bbb")
    model_at(hub / "models--Systran--faster-whisper-small/snapshots/ccc")
    (small.parent.parent / "refs").mkdir()
    (small.parent.parent / "refs/main").write_text("bbb")
    assert find_local_model(tmp_path, home=tmp_path, environment={}) == {
        "path": str(small.resolve()), "name": "faster-whisper-small", "source": "huggingface-cache"}


def test_custom_hf_cache_and_environment_model(tmp_path):
    small = model_at(tmp_path / "custom/models--Systran--faster-whisper-small/snapshots/aaa")
    assert find_local_model(tmp_path, home=tmp_path, environment={"HF_HUB_CACHE": str(tmp_path / "custom")})["path"] == str(small)
    explicit = model_at(tmp_path / "explicit")
    assert find_local_model(tmp_path, home=tmp_path, environment={"EASEL_ASR_MODEL_PATH": str(explicit)})["source"] == "configured"


def test_project_model_wins_and_incomplete_explicit_does_not_fall_back(tmp_path):
    cached = model_at(tmp_path / ".cache/easel-models/faster-whisper-small")
    selected = model_at(tmp_path / "selected")
    assert find_local_model(tmp_path, settings={"asr_model_path": "selected"}, home=tmp_path, environment={})["path"] == str(selected)
    (selected / "tokenizer.json").unlink()
    with pytest.raises(ValueError, match="不完整"):
        find_local_model(tmp_path, settings={"asr_model_path": "selected"}, home=tmp_path, environment={})
    assert find_local_model(tmp_path, home=tmp_path, environment={})["path"] == str(cached)


def test_missing_or_partial_cache_does_not_request_a_download(tmp_path):
    partial = tmp_path / ".cache/easel-models/faster-whisper-small"
    partial.mkdir(parents=True)
    (partial / "model.bin").write_bytes(b"weights")
    assert find_local_model(tmp_path, home=tmp_path, environment={}) is None


@pytest.fixture
def transcription(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    pid = "wf-0123456789ab"
    directory = root / "outputs/视频工作流" / pid
    directory.mkdir(parents=True)
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"read only input")
    project = {"id": pid, "kind": "video", "title": "转录验证", "settings": {}, "media": {},
               "manuscripts": [{"id": "main", "title": "选定稿", "content": "Remotion 是真实术语", "version": 1},
                               {"id": "other", "title": "其他稿", "content": "不要使用这份参考稿", "version": 1}],
               "primary_manuscript_id": "main"}
    original = copy.deepcopy(project)
    runner.write_json(directory / "artifacts/source-metadata.json", {"duration": 10, "source_path": str(source), "source_sha256": "source-hash"})
    runner.write_json(directory / "sdk/run-state.json", {"stages": {}, "config": {}})
    local = model_at(tmp_path / "models/faster-whisper-small")
    monkeypatch.setattr("easel.workflow_asr.find_local_model", lambda *a, **kw: {"path": str(local), "name": "small", "source": "cache"})
    async def stage(self, *args, **kwargs):
        config = runner.read_json(self.sdk_run / "run-state.json")["config"]
        runner.write_json(self.sdk_run / "artifacts/transcript.json", runner.read_json(Path(config["transcript"])))
    monkeypatch.setattr(runner._Run, "sdk_command", stage)
    skill = root / "skills/openclaw/video-production/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: video-production\nlayer: produce\n---\n转录原录音。", encoding="utf-8")
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(root / "isolated-profile"))
    monkeypatch.delenv("EASEL_OPENCLAW_WORKSPACE", raising=False)
    return root, directory, project, original, source


def raw_transcript(source):
    return {"source": str(source.resolve()), "duration": 10,
            "segments": [{"start": 1.2, "end": 3.7, "text": "瑞莫神", "words": [{"word": "瑞莫神", "start": 1.2, "end": 3.7}]}, {"start": 4.1, "end": 9.3, "text": "录音原话"}]}


@pytest.mark.parametrize("field", ["transcript_path", "transcript_reference_path"])
@pytest.mark.parametrize("extension", [".txt", ".md"])
def test_plain_text_is_a_read_only_reference_and_audio_supplies_timestamps(transcription, field, extension):
    root, directory, project, _, source = transcription
    reference = root / ("校对稿" + extension)
    reference.write_text("Remotion 的校对参考文本", encoding="utf-8-sig")
    original_bytes = reference.read_bytes()
    project["media"][field] = str(reference)
    original = json.loads(json.dumps(project))
    calls, events = [], []
    class Agent:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            assert "Remotion 的校对参考文本" in kwargs["context"]["reference_manuscript"]
            assert "Remotion 是真实术语" in kwargs["context"]["reference_manuscript"]
            assert kwargs["context"]["command_argv"][kwargs["context"]["command_argv"].index("--src") + 1] == str(source)
            runner.write_json(kwargs["directory"] / "transcript.json", raw_transcript(source))
            runner.write_json(kwargs["directory"] / "corrections.json", {"corrections": [{"index": 0, "text": "Remotion"}], "unsupported_requests": []})
    result = asyncio.run(runner.WorkflowRunner(root, skill_agent=Agent()).execute(project, "transcript", {}, {}, directory, events.append))
    assert result["status"] == "completed" and len(calls) == 1
    assert project == original and reference.read_bytes() == original_bytes
    report = runner.read_json(directory / "artifacts/transcription-report.json")
    assert report["reference_file"]["path"] == str(reference)
    assert report["reference_manuscript"]["id"] == "main"
    final = runner.read_json(directory / "artifacts/transcript.json")
    assert [(s["start"], s["end"]) for s in final["segments"]] == [(1.2, 3.7), (4.1, 9.3)]
    assert final["segments"][0]["text"] == "Remotion"
    assert result["media"]["transcript_reference_path"] == str(reference)
    if field == "transcript_path":
        assert result["media"]["transcript_path"] == ""
    assert any(isinstance(e, dict) and e.get("phase") == "transcribing" for e in events)


def test_existing_timestamped_subtitles_keep_their_times_with_reference_file(transcription):
    root, directory, project, _, source = transcription
    supplied = runner.write_json(root / "existing.json", raw_transcript(source))
    reference = root / "reference.txt"
    reference.write_text("校对参考", encoding="utf-8")
    project["media"] = {"transcript_path": str(supplied), "transcript_reference_path": str(reference)}
    class NoAgent:
        async def execute(self, **kwargs):
            pytest.fail("Existing timed subtitles must not run ASR again")
    result = asyncio.run(runner.WorkflowRunner(root, skill_agent=NoAgent()).execute(project, "transcript", {}, {}, directory, None))
    assert result["status"] == "completed"
    final = runner.read_json(directory / "artifacts/transcript.json")
    assert [(s["start"], s["end"]) for s in final["segments"]] == [(1.2, 3.7), (4.1, 9.3)]


@pytest.mark.parametrize("content", ["", "\x00binary"])
def test_unusable_plain_reference_stops_with_a_specific_reason(transcription, content):
    root, directory, project, _, _ = transcription
    reference = root / "reference.txt"
    reference.write_text(content, encoding="utf-8")
    project["media"]["transcript_path"] = str(reference)
    result = asyncio.run(runner.WorkflowRunner(root).execute(project, "transcript", {}, {}, directory, None))
    assert result["status"] == "blocked" and "校对稿" in result["message"]


@pytest.mark.parametrize("bad", [None, "timestamp", "missing_file", "wrong_source"])
def test_original_agent_transcribes_reads_selected_manuscript_and_host_validates(transcription, bad):
    root, directory, project, original, source = transcription
    calls, events = [], []
    class Agent:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["node"] == "transcript"
            assert kwargs["context"]["reference_manuscript"] == "Remotion 是真实术语"
            assert "--local-files-only" in kwargs["context"]["command_argv"]
            reference = Path(kwargs["context"]["reference_path"])
            assert reference.read_text(encoding="utf-8").strip() == "Remotion 是真实术语"
            assert kwargs["skill"]["source_path"] == str(root / "skills/openclaw/video-production/SKILL.md")
            kwargs["on_event"]({"kind": "tool", "text": "工具开始：exec"})
            data = raw_transcript(source)
            if bad == "wrong_source":
                data["source"] = "different.mp4"
            if bad != "missing_file":
                runner.write_json(kwargs["directory"] / "transcript.json", data)
            item = {"index": 0, "text": "Remotion"}
            if bad == "timestamp":
                item["start"] = 0
            runner.write_json(kwargs["directory"] / "corrections.json", {"corrections": [item], "unsupported_requests": []})
            return {"text": "原 Agent 实际转录并校对"}
    result = asyncio.run(runner.WorkflowRunner(root, skill_agent=Agent()).execute(project, "transcript", {}, {}, directory, events.append))
    assert len(calls) == 1 and project == original and source.read_bytes() == b"read only input"
    assert any(e.get("text") == "工具开始：exec" for e in events if isinstance(e, dict))
    if bad:
        assert result["status"] == "blocked"
        assert not (directory / "artifacts/transcript.json").exists()
    else:
        final = runner.read_json(directory / "artifacts/transcript.json")
        assert [s["text"] for s in final["segments"]] == ["Remotion", "录音原话"]
        assert [(s["start"], s["end"]) for s in final["segments"]] == [(1.2, 3.7), (4.1, 9.3)]
        assert runner.read_json(directory / "artifacts/raw-transcript.json")["segments"][0]["text"] == "瑞莫神"
        assert runner.read_json(directory / "artifacts/raw-transcript.json")["segments"][0]["words"] == raw_transcript(source)["segments"][0]["words"]
        assert runner.read_json(directory / "artifacts/transcription-report.json")["reference_manuscript"]["id"] == "main"
        assert result["media"]["generated_transcript_path"].endswith("transcript.json")


def test_host_fallback_transcription_still_passes_reference_and_corrects_timed_text(transcription, monkeypatch):
    root, directory, project, original, source = transcription
    async def command(self, args, **kwargs):
        assert "--local-files-only" in args and "--reference-file" in args
        runner.write_json(Path(args[args.index("--out") + 1]), raw_transcript(source))
    async def generate(prompt, **kwargs):
        assert "Remotion 是真实术语" in prompt and "不要使用这份参考稿" not in prompt
        return {"corrections": [{"index": 0, "text": "Remotion"}], "unsupported_requests": []}
    monkeypatch.setattr(runner._Run, "command", command)
    monkeypatch.setattr(runner, "generate", generate)
    result = asyncio.run(runner.WorkflowRunner(root).execute(project, "transcript", {}, {}, directory, lambda e: None))
    assert result["status"] == "completed" and project == original


def test_transcribe_cli_uses_offline_model_and_reference_but_keeps_audio_timestamps(tmp_path, monkeypatch):
    source = tmp_path / "recording.mp4"
    reference = tmp_path / "reference.md"
    reference.write_text("Remotion 专有名词", encoding="utf-8")
    output = tmp_path / "transcript.json"
    calls = []
    class Model:
        def __init__(self, name, **kwargs):
            assert kwargs["local_files_only"] is True
        def transcribe(self, src, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(start=1.25, end=2.75, text="实际音频", words=[])], SimpleNamespace(language="zh", duration=3)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(sys, "argv", ["transcribe.py", "--src", str(source), "--out", str(output), "--model", str(tmp_path), "--reference-file", str(reference), "--local-files-only"])
    script = Path(__file__).parents[1] / "skills/openclaw/video-production/vendor/video-pipeline-sdk/tools/transcribe.py"
    runpy.run_path(str(script), run_name="__main__")
    assert calls[0]["initial_prompt"] == "Remotion 专有名词" and calls[0]["word_timestamps"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["segments"] == [{"start": 1.25, "end": 2.75, "text": "实际音频", "words": []}]


def test_execute_step_automatically_receives_same_original_agent_as_chat(tmp_path, monkeypatch):
    from easel.content_workflow import ContentWorkflowService
    seen = []
    agent = object()
    class Executor:
        def __init__(self, root, *, skill_agent):
            seen.append(skill_agent)
        async def execute(self, *args):
            return {"status": "completed", "message": "完成", "artifacts": []}
    monkeypatch.setattr(runner, "WorkflowRunner", Executor)
    async def run():
        service = ContentWorkflowService(tmp_path / "project", vault=tmp_path / "vault")
        service.skill_agent = agent
        p = service.create({"title": "执行入口验证"})
        await service.run(p["id"], "brief", {})
        await service.tasks[p["id"]]
        assert service.get(p["id"])["nodes"][0]["status"] == "completed"
    asyncio.run(run())
    assert seen == [agent]
