import asyncio
from pathlib import Path
import shutil
import sys

import pytest

from easel import workflow_runner as module


@pytest.fixture
def render_case(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    directory = root / "outputs/视频工作流/render-test"
    work = directory / "remotion"
    template = root / "assets/workflow-template"
    original_template = Path(__file__).resolve().parents[1] / "assets/workflow-template"
    shutil.copytree(original_template, template)
    shutil.copytree(template, work)
    (work / "public").mkdir()
    source = work / "public/source.mp4"
    source.write_bytes(b"unchanged source audio and video")
    props = {"source": source.name, "source_sha256": module.file_sha256(source),
             "duration": 20, "fps": 30, "width": 1080, "height": 1920,
             "visual": {"accent": "#2F6FED"}, "captions": [{"text": "原字幕", "startMs": 0, "endMs": 2000}],
             "scenes": [{"start": 0, "end": 10, "card": ""}, {"start": 10, "end": 20, "card": "已确认的卡片"}]}
    module.write_json(work / "props.json", props)
    module.write_json(work / "template-ownership.json", {
        path.relative_to(work).as_posix(): module.file_sha256(path)
        for path in (work / "src").rglob("*") if path.is_file()})
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(root / "isolated-openclaw"))
    monkeypatch.delenv("EASEL_OPENCLAW_WORKSPACE", raising=False)
    events = []
    runner = module._Run(module.WorkflowRunner(root), {"id": "render-test", "kind": "video"},
                         "review", {}, {}, directory, events.append)
    return runner, props, events


def test_owned_template_refresh_keeps_confirmed_props_and_backup(render_case):
    runner, _, events = render_case
    root_tsx = runner.work / "src/Root.tsx"
    old_source = b"// previous host-owned template\n"
    root_tsx.write_bytes(old_source)
    ownership = module.read_json(runner.work / "template-ownership.json")
    ownership["src/Root.tsx"] = module.file_sha256(root_tsx)
    module.write_json(runner.work / "template-ownership.json", ownership)
    props_before = (runner.work / "props.json").read_bytes()
    source_before = (runner.work / "public/source.mp4").read_bytes()
    assert runner.sync_template_sources(require_existing=True)
    assert (runner.work / "props.json").read_bytes() == props_before
    assert (runner.work / "public/source.mp4").read_bytes() == source_before
    backup = list((runner.artifacts / "review").glob("template-before-*/src/Root.tsx"))
    assert len(backup) == 1 and backup[0].read_bytes() == old_source
    assert module.read_json(runner.work / "template-ownership.json")["src/Root.tsx"] == module.file_sha256(root_tsx)
    assert any("保留原片、字幕、分镜" in e["text"] for e in events)
    assert not runner.sync_template_sources(require_existing=True)


@pytest.mark.parametrize("edit", ["change", "new_file", "delete"])
def test_template_refresh_never_overwrites_manual_source(render_case, edit):
    runner, _, _ = render_case
    path = runner.work / "src" / ("Manual.tsx" if edit == "new_file" else "Root.tsx")
    if edit == "delete":
        path.unlink()
    else:
        path.write_bytes(b"// manual edit must survive\n")
    before = {str(p): p.read_bytes() for p in (runner.work / "src").rglob("*") if p.is_file()}
    with pytest.raises(module.RunnerBlocked, match="停止覆盖"):
        runner.sync_template_sources(require_existing=True)
    assert {str(p): p.read_bytes() for p in (runner.work / "src").rglob("*") if p.is_file()} == before


def install_review_tools(monkeypatch, calls, *, missing_frame=False, render_error=False, decode_error=False, duration=12):
    async def remotion(self, command, *args, **kwargs):
        calls.append((command, args, kwargs))
        if render_error:
            raise module.RunnerBlocked("Remotion render失败（exit=1）")
        output = Path(args[1])
        if "--sequence" in args:
            output.mkdir(parents=True)
            frames = [int(frame) for frame in args[args.index("--frames") + 1].split(",")]
            pad = len(str(max(frames)))
            for frame in frames[1:] if missing_frame else frames:
                (output / f"frame-{frame:0{pad}d}.png").write_bytes(b"test image")
        else:
            output.write_bytes(b"new preview with audio and video")
        return 0, "success"

    async def probe(self, path):
        return {"duration": duration, "width": 540, "height": 960, "audio": True, "fps": "30/1"}

    async def command(self, args, **kwargs):
        calls.append(("decode", args, kwargs))
        assert args[0] == "ffmpeg" and "-xerror" in args
        if decode_error:
            raise module.RunnerBlocked("完整解码样片失败（exit=1）")
        return 0, ""

    monkeypatch.setattr(module._Run, "remotion", remotion)
    monkeypatch.setattr(module._Run, "probe", probe)
    monkeypatch.setattr(module._Run, "command", command)


def test_review_renders_one_batch_with_real_frame_numbers_and_decode_gate(render_case, monkeypatch):
    runner, props, _ = render_case
    calls = []
    install_review_tools(monkeypatch, calls)
    result = asyncio.run(runner.execute())
    assert result["status"] == "awaiting_review"
    assert [call[0] for call in calls] == ["render", "decode", "render"]
    render, _, boundary = calls
    assert render[1][render[1].index("--frames") + 1] == "240-599"
    assert boundary[1][boundary[1].index("--frames") + 1] == "0,1,299,300,301,599"
    assert "--sequence" in boundary[1] and "--muted" in boundary[1]
    assert len([a for a in result["artifacts"] if a["kind"] == "inspection_frame"]) == 6
    report = module.read_json(next(Path(a["path"]) for a in result["artifacts"] if a["name"] == "样片检查报告"))
    assert report["source_range_seconds"] == [8, 20]
    assert report["decode"] == "passed" and report["human_visual_review"] == "pending"
    assert module.read_json(runner.work / "props.json") == props


@pytest.mark.parametrize("problem", ["missing_frame", "render_error", "decode_error", "wrong_duration"])
def test_review_failure_never_promotes_stale_outputs(render_case, monkeypatch, problem):
    runner, _, _ = render_case
    old = runner.artifacts / "review/old-success/frames"
    old.mkdir(parents=True)
    (old.parent / "preview.mp4").write_bytes(b"previous preview")
    (old / "frame-000.png").write_bytes(b"previous frame")
    install_review_tools(monkeypatch, [], **({"duration": 30} if problem == "wrong_duration" else {problem: True}))
    result = asyncio.run(runner.execute())
    assert result["status"] == "blocked"
    assert not any(a["kind"] in {"preview_video", "inspection_frame"} for a in result["artifacts"])
    assert not list((runner.artifacts / "review").glob("run-*/review-report.json"))
    assert (old.parent / "preview.mp4").read_bytes() == b"previous preview"


def test_command_streams_progress_before_process_finishes_and_keeps_output_private(render_case):
    runner, _, _ = render_case
    received = []
    first = asyncio.Event()

    def on_output(line):
        received.append(line)
        if "Rendered 2/3" in line:
            first.set()

    async def run():
        script = "import sys,time; sys.stdout.write('Rendered 2/3\\r'); sys.stdout.flush(); time.sleep(2); print('Rendered 3/3')"
        task = asyncio.create_task(runner.command([sys.executable, "-c", script], on_output=on_output))
        await asyncio.wait_for(first.wait(), 1.8)
        assert not task.done()
        await task

    asyncio.run(run())
    assert "Rendered 2/3" in received and "Rendered 3/3" in received


@pytest.mark.parametrize("line,expected", [
    ("\x1b[32mRendered 200/360, time remaining: 5s private-token\x1b[0m", ("已渲染 200/360 帧（56%）", False)),
    ("Encoded 360/360", ("已编码 360/360 帧（100%）", True)),
    ("Bundling 100% private-token", ("正在准备渲染代码：100%", True)),
    ("Copying public dir 214.1 MB private-token", ("正在准备视频素材：214.1 MB", False)),
    ("Rendered 99/0", None), ("Rendered 10/3", None),
    ("Browser log: api_key=private-token", None),
])
def test_remotion_progress_exposes_only_valid_counters(line, expected):
    assert module.remotion_progress(line) == expected


def test_render_nodes_read_the_reusable_render_guide(render_case, monkeypatch):
    runner, _, _ = render_case
    skill = runner.root / "skills/openclaw/video-production"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: video-production\ndescription: video\n---\n渲染规范入口 [指南](references/workflow-render.md)", encoding="utf-8")
    (skill / "references").mkdir()
    (skill / "references/workflow-render.md").write_text("原生取帧，完整解码后等待用户验收", encoding="utf-8")
    (skill / "references/workflow-subtitles.md").write_text("不应读取字幕节点专用指南", encoding="utf-8")
    for node in ("build", "review", "deliver"):
        runner.node = node
        runner.library_skills_used = []
        guide = runner.read_library_guidance()
        assert "原生取帧" in guide and "不应读取" not in guide
        assert {entry["path"] for entry in runner.library_skills_used} == {"SKILL.md", "references/workflow-render.md"}
