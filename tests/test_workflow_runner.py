import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from easel import workflow_runner as module
from easel.workflow_runner import RunnerBlocked, WorkflowRunner, validate_timeline, validate_transcript, validate_visual


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "repo"
    directory = root / "outputs/视频工作流/test-run"
    directory.mkdir(parents=True)
    project = {"id":"test-run", "kind":"video", "title":"固定流程的价值", "content_version":1,
               "manuscripts":[{"id":"m1","title":"原稿","content":"用户自己的稿件","version":1,"is_primary":True}],
               "primary_manuscript_id":"m1","nodes":[],"settings":{},"media":{}}
    return root, directory, project


def execute(case, node, **options):
    root, directory, project = case
    return asyncio.run(WorkflowRunner(root).execute(project,node,options,{"content":"测试节点规范"},directory,lambda _:None))


def test_script_adopts_existing_and_preserves_every_manuscript(case, monkeypatch):
    async def no_model(*args, **kwargs):
        pytest.fail("Adopting a manuscript must not spend a model request")
    monkeypatch.setattr(module,"generate",no_model)
    result = execute(case,"script",action="adopt")
    assert result["status"] == "awaiting_review"
    assert result["manuscripts"] == case[2]["manuscripts"]
    assert Path(result["artifacts"][0]["path"]).read_text(encoding="utf-8") == "用户自己的稿件"


def test_script_generate_appends_without_rewriting_source(case, monkeypatch):
    systems = []
    async def model(prompt, *, system):
        systems.append(system)
        return "依据意见生成的新稿"
    monkeypatch.setattr(module,"generate",model)
    result = execute(case,"script",mode="generate")
    assert len(systems)==1 and "测试节点规范" in systems[0] and "没有工具" in systems[0]
    assert len(result["manuscripts"]) == 2
    assert result["manuscripts"][0]["content"] == "用户自己的稿件"
    assert result["manuscripts"][1]["content"] == "依据意见生成的新稿"
    assert case[2]["manuscripts"][0]["is_primary"] is True
    assert result["primary_manuscript_id"] != "m1"


def test_generation_reads_additional_obsidian_manuscripts(case,monkeypatch):
    case[2]["manuscripts"].append({"id":"m2","title":"Obsidian参考","source_path":"notes/ref.md","content":"不可忽略的第二来源","version":1})
    async def model(prompt,*,system):
        assert "不可忽略的第二来源" in prompt and "notes/ref.md" in prompt
        return "综合稿"
    monkeypatch.setattr(module,"generate",model)
    assert execute(case,"script",mode="generate")["status"]=="awaiting_review"


def test_article_publication_package_does_not_require_video(case):
    case[2]["kind"]="article"
    result=execute(case,"publish",action="run")
    assert result["status"]=="completed"
    assert "未提交" in result["message"]
    assert execute(case,"publish",action="publish")["status"]=="blocked"


def test_deliver_does_not_implicitly_reimport_prior_final(case,monkeypatch):
    old=case[1]/"old-final.mp4"
    old.write_bytes(b"old-video")
    case[2]["media"]["final_path"]=str(old)
    public=case[1]/"remotion/public"
    public.mkdir(parents=True)
    (public/"source.mp4").write_bytes(b"source")
    module.write_json(case[1]/"remotion/props.json",{"source":"source.mp4","source_sha256":module.file_sha256(public/"source.mp4")})
    called=[]
    async def remotion(self,*args,**kwargs):
        called.append(args)
        (self.artifacts/"final.mp4").write_bytes(b"new-video")
    async def probe(*args):return {"duration":4,"width":640,"height":360}
    async def command(self,args,**kwargs):
        if "-frames:v" in args:Path(args[-1]).write_bytes(b"cover")
        return 0,"passed"
    monkeypatch.setattr(module._Run,"remotion",remotion)
    monkeypatch.setattr(module._Run,"probe",probe)
    monkeypatch.setattr(module._Run,"command",command)
    result=execute(case,"deliver")
    assert called and result["status"]=="awaiting_review"
    assert (case[1]/"artifacts/final.mp4").read_bytes()==b"new-video"


def test_storyboard_refuses_transcript_from_other_source(case):
    module.write_json(case[1]/"artifacts/source-metadata.json",{"duration":10,"source_sha256":"new"})
    module.write_json(case[1]/"artifacts/transcript.json",{"source_sha256":"old","segments":[{"start":0,"end":10,"text":"字幕"}]})
    result=execute(case,"storyboard",timeline={"scenes":[{"title":"章","start":0,"end":10,"purpose":"目的","card":""}]})
    assert result["status"]=="blocked" and "其他版本原片" in result["message"]


def test_publication_idempotency_binds_exact_video_bytes(case,monkeypatch):
    directory=case[1]
    (directory/"artifacts").mkdir()
    final=directory/"artifacts/final.mp4"
    final.write_bytes(b"first version")
    calls=[]
    async def probe(*args):return {"duration":10}
    async def command(self,args,**kwargs):
        if "--exec" in args:calls.append(args)
        return 0,"草稿箱标题回读已确认"
    monkeypatch.setattr(module._Run,"probe",probe)
    monkeypatch.setattr(module._Run,"command",command)
    assert execute(case,"publish",action="draft")["status"]=="completed"
    first=module.read_json(directory/"artifacts/publication-receipt.json")
    assert execute(case,"publish",action="draft")["status"]=="completed"
    assert len(calls)==1
    case[2]["content_version"]=2
    assert execute(case,"publish",action="draft")["status"]=="completed"
    assert len(calls)==1
    rebound=module.read_json(directory/"artifacts/publication-receipt.json")
    assert rebound["content_version"]==2 and rebound["reused_from_version"]==1
    assert Path(rebound["reused_from_receipt"]).is_file()
    final.write_bytes(b"second different version")
    assert execute(case,"publish",action="draft")["status"]=="completed"
    assert len(calls)==2
    assert first["publication_hash"] != module.read_json(directory/"artifacts/publication-receipt.json")["publication_hash"]


def test_model_failure_does_not_create_fake_manuscript(case, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(module,"generate",broken)
    result = execute(case,"script",mode="generate")
    assert result["status"] == "failed"
    assert not list((case[1]/"artifacts").glob("manuscript-*.md"))


def test_generation_progress_streams_only_text_as_throttled_snapshots(case,monkeypatch):
    events=[]
    parts=["这是", "可查看的正文", "。", " api_key=private-secret-value"]
    async def model(prompt,*,system,on_text):
        on_text(parts[0])
        assert events[-1]=={"kind":"generation","text":"这是"}
        # An unexpected metadata object is never serialized to a UI snapshot.
        on_text({"reasoning":"INTERNAL_REASONING_MUST_NOT_APPEAR"})
        for part in parts[1:]:
            on_text(part)
        assert len([e for e in events if isinstance(e,dict) and e['kind']=='generation'])==1
        return ''.join(parts)
    monkeypatch.setattr(module,"generate",model)
    root,directory,project=case
    runner=module._Run(WorkflowRunner(root),project,"script",{}, {"content":"仅生成正文"},directory,events.append)
    result=asyncio.run(runner.model("写一段稿件"))
    snapshots=[event['text'] for event in events if isinstance(event,dict) and event['kind']=='generation']
    assert len(snapshots)==2
    assert snapshots[-1]=="这是可查看的正文。 api_key=[REDACTED]"
    assert "INTERNAL_REASONING" not in json.dumps(events)
    assert result==''.join(parts)  # Display sanitization never rewrites the artifact.


@pytest.mark.parametrize("node,budget,expected",[
    ("script",None,"large"),("brief","standard","standard"),
    ("transcript","large","large"),("storyboard","maximum","maximum"),
    ("build","maximum","maximum"),("build","standard","standard"),
])
def test_generation_budget_reaches_adapter_for_every_creative_node(case,monkeypatch,node,budget,expected):
    from easel import workflow_model
    calls=[]
    async def adapter(prompt,*,system,on_text,generation_budget,task=None):
        calls.append({"budget":generation_budget,"task":task})
        on_text("真实正文")
        return "真实正文"
    monkeypatch.setattr(workflow_model,"generate",adapter)
    root,directory,project=case
    if budget is not None:
        project["settings"]["generation_budget"]=budget
    events=[]
    runner=module._Run(WorkflowRunner(root),project,node,{}, {},directory,events.append)
    assert asyncio.run(runner.model("生成测试产物"))=="真实正文"
    assert calls==[{"budget":expected,"task":"short_json" if node=="build" else None}]
    assert {"kind":"generation","text":"真实正文"} in events


@pytest.mark.parametrize("exit_code",[0,7])
def test_tool_progress_reports_actual_exit_without_command_or_stdout(case,exit_code):
    events=[]
    root,directory,project=case
    runner=module._Run(WorkflowRunner(root),project,"brief",{}, {},directory,events.append)
    args=[module.sys.executable,"-c",f"print('private-tool-output');raise SystemExit({exit_code})"]
    actual,_=asyncio.run(runner.command(args,label="媒体检查",check=False))
    assert actual==exit_code
    assert events==[{"kind":"tool","text":"开始：媒体检查"},{"kind":"result","text":f"媒体检查执行结束（退出码 {exit_code}）。"}]
    assert "private-tool-output" not in json.dumps(events)
    assert module.sys.executable not in json.dumps(events)


@pytest.mark.parametrize("segments", [[],[{"start":0,"end":11,"text":"越界"}],[{"start":0,"end":2,"text":"第一句"},{"start":1,"end":3,"text":"重叠"}],[{"start":0,"end":1,"text":""}]])
def test_subtitle_validation_rejects_missing_overlap_and_source_overrun(segments):
    with pytest.raises(RunnerBlocked):
        validate_transcript({"segments":segments},10)


def test_storyboard_rejects_code_sparse_gaps_and_excess_cards():
    valid = {"scenes":[{"title":"开场","start":0,"end":10,"purpose":"解释","card":"一个关键点"}]}
    assert validate_timeline(valid,10)["timeline_mode"] == "original_no_cuts"
    for bad in [
        {"scenes":[{**valid["scenes"][0],"code":"system()"}]},
        {"scenes":[{**valid["scenes"][0],"start":1}]},
        {"scenes":[{**valid["scenes"][0],"end":5},{**valid["scenes"][0],"start":5}]},
    ]:
        with pytest.raises(RunnerBlocked):
            validate_timeline(bad,10)


def test_visual_props_cannot_inject_css_or_commands():
    props = {"background":"#111111","accent":"#FFAA33","textColor":"#FFFFFF","subtitleSize":48,"cardPosition":"left","titleCase":"bold"}
    assert validate_visual(props,"documentary")["template"] == "documentary"
    with pytest.raises(RunnerBlocked):
        validate_visual({**props,"background":"url(https://example.com)"},"documentary")
    with pytest.raises(RunnerBlocked):
        validate_visual({**props,"command":"curl"},"documentary")


def test_caption_display_wrap_never_changes_words_or_timing():
    text="字幕显示只做视觉换行而不改变原本真实的时间戳也绝对不能为了双行去丢掉最后几个文字"
    source=[{"text":text,"startMs":1234,"endMs":6789,"timestampMs":None,"confidence":None}]
    displayed,long_indices=module.display_captions(source)
    assert long_indices==[0]
    assert all(len(line)<=18 for line in displayed[0]['text'].split('\n'))
    assert displayed[0]['text'].replace('\n','')==text
    assert displayed[0]['startMs']==1234 and displayed[0]['endMs']==6789
    assert source[0]['text']==text


def test_build_explicit_and_builtin_defaults_skip_model(case,monkeypatch):
    async def no_model(*args,**kwargs):pytest.fail("Explicit and built-in visual parameters need no model")
    monkeypatch.setattr(module,"generate",no_model)
    root,directory,project=case
    explicit={"background":"#102020","accent":"#DDAA33","textColor":"#FFFFFF","subtitleSize":54,"cardPosition":"left","titleCase":"bold"}
    runner=module._Run(WorkflowRunner(root),project,"build",{}, {"source":"builtin","personalized":False,"content":"完整内置规则"},directory,lambda _:None)
    visual,origin=asyncio.run(runner.visual_parameters({"visual_parameters":explicit},"documentary"))
    assert origin=="explicit" and visual['subtitleSize']==54
    visual,origin=asyncio.run(runner.visual_parameters({"visual_style":"克制科技纪录片","subtitle_style":"清晰双行"},"documentary"))
    assert origin=="default" and visual['subtitleSize']==48


def test_build_feedback_uses_explicit_parameters_as_baseline_not_override(case,monkeypatch):
    explicit={"background":"#102020","accent":"#DDAA33","textColor":"#FFFFFF","subtitleSize":54,"cardPosition":"left","titleCase":"bold"}
    async def model(prompt,*,system,task):
        assert task=="short_json"
        assert "#DDAA33" in prompt and "修改前参考基线" in prompt and "强调色改为红色" in prompt
        return {**explicit,"accent":"#CC3333"}
    monkeypatch.setattr(module,"generate",model)
    root,directory,project=case
    runner=module._Run(WorkflowRunner(root),project,"build",{"feedback":"强调色改为红色"},{"source":"builtin","personalized":False,"content":"保留未要求改变的视觉属性"},directory,lambda _:None)
    visual,origin=asyncio.run(runner.visual_parameters({"visual_parameters":explicit},"documentary"))
    assert origin=="model" and visual["accent"]=="#CC3333"
    assert explicit["accent"]=="#DDAA33"


@pytest.mark.parametrize("personal,feedback,style",[(True,None,"克制科技纪录片"),(False,"字体再大些","克制科技纪录片"),(False,None,"米白色杂志风")])
def test_build_custom_preferences_use_short_json_model(case,monkeypatch,personal,feedback,style):
    calls=[]
    async def model(prompt,*,system,task):
        calls.append((prompt,system,task))
        return {"background":"#102020","accent":"#DDAA33","textColor":"#FFFFFF","subtitleSize":54,"cardPosition":"left","titleCase":"bold"}
    monkeypatch.setattr(module,"generate",model)
    root,directory,project=case
    runner=module._Run(WorkflowRunner(root),project,"build",{"feedback":feedback}, {"source":"personal" if personal else "builtin","personalized":personal,"content":"完整节点Skill"},directory,lambda _:None)
    _,origin=asyncio.run(runner.visual_parameters({"visual_style":style,"subtitle_style":"清晰双行"},"documentary"))
    assert origin=="model" and calls[0][2]=="short_json"
    assert "完整节点Skill" in calls[0][1] and "没有工具" in calls[0][1]


def test_missing_asr_blocks_without_spawning_download(case,monkeypatch):
    directory = case[1]
    (directory/"artifacts").mkdir()
    (directory/"sdk").mkdir()
    (directory/"artifacts/source-metadata.json").write_text('{"duration":10,"source_path":"unused.mp4"}',encoding="utf-8")
    (directory/"sdk/run-state.json").write_text('{"stages":{},"config":{}}',encoding="utf-8")
    monkeypatch.delenv("SILICONFLOW_API_KEY",raising=False)
    async def no_process(*args, **kwargs):
        pytest.fail("No ASR subprocess may start without a source or configured model")
    monkeypatch.setattr(module._Run,"command",no_process)
    result = execute(case,"transcript")
    assert result["status"] == "blocked"
    assert "不会自动下载" in result["message"]


def test_transcript_feedback_only_changes_text_and_preserves_source(case,monkeypatch):
    directory=case[1]
    source=directory/"provided.json"
    original={"segments":[{"start":0,"end":5,"text":"瑞莫神"},{"start":5,"end":10,"text":"固定时间轴"}]}
    module.write_json(source,original)
    case[2]["media"]["transcript_path"]=str(source)
    module.write_json(directory/"artifacts/source-metadata.json",{"duration":10,"source_sha256":"same-source"})
    module.write_json(directory/"sdk/run-state.json",{"stages":{},"config":{}})
    async def sdk(self,*args,**kwargs):
        module.write_json(self.sdk_run/"artifacts/transcript.json",original)
    async def model(prompt,*,system):
        assert "改成 Remotion" in prompt
        return {"corrections":[{"index":0,"text":"Remotion"}],"unsupported_requests":[]}
    monkeypatch.setattr(module._Run,"sdk_command",sdk)
    monkeypatch.setattr(module,"generate",model)
    result=execute(case,"transcript",feedback="瑞莫神改成 Remotion")
    assert result["status"]=="completed"
    generated=module.read_json(directory/"artifacts/transcript.json")
    assert [(s['start'],s['end']) for s in generated['segments']]==[(0,5),(5,10)]
    assert generated['segments'][0]['text']=="Remotion"
    assert module.read_json(source)==original
    assert result['media']['transcript_path']==str(source)
    assert result['media']['generated_transcript_path']!=str(source)


def test_logs_redact_credentials_and_do_not_execute_shell(case,monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY","a-real-looking-private-key")
    assert "a-real-looking-private-key" not in module.clean_log("request a-real-looking-private-key password=abc123456 bearer secrettoken123")
    result = execute(case,"brief")
    assert result["status"] == "awaiting_review"
    assert Path(result["artifacts"][-1]["path"]).is_file()


def test_unknown_receipt_blocks_repeat_publish(case,monkeypatch):
    directory=case[1]
    (directory/"artifacts").mkdir()
    (directory/"artifacts/final.mp4").write_bytes(b"video")
    (directory/"artifacts/publication-receipt.json").write_text('{"outcome":"unknown","platform":"weixin-channels"}',encoding="utf-8")
    async def probe(*args):return {"duration":10}
    async def no_process(*args,**kwargs):pytest.fail("Uncertain publication must never automatically resubmit")
    monkeypatch.setattr(module._Run,"probe",probe)
    monkeypatch.setattr(module._Run,"command",no_process)
    result=execute(case,"publish",action="publish")
    assert result["status"]=="blocked" and result["publication_uncertain"] is True
    assert result["manual_verification_url"]=="https://channels.weixin.qq.com/"


def test_publication_timeout_keeps_durable_unknown_receipt(case,monkeypatch):
    directory=case[1]
    (directory/"artifacts").mkdir()
    (directory/"artifacts/final.mp4").write_bytes(b"video")
    async def probe(*args):return {"duration":10}
    async def command(self,args,**kwargs):
        if "--exec" in args:raise TimeoutError("timed out")
        return 0,"dry run passed"
    monkeypatch.setattr(module._Run,"probe",probe)
    monkeypatch.setattr(module._Run,"command",command)
    result=execute(case,"publish",action="draft")
    assert result["status"]=="failed" and result["publication_uncertain"] is True
    receipt=json.loads((directory/"artifacts/publication-receipt.json").read_text(encoding="utf-8"))
    assert receipt["outcome"]=="unknown" and receipt["verified"] is False


def test_successful_exit_without_readback_is_not_published(case,monkeypatch):
    directory=case[1]
    (directory/"artifacts").mkdir()
    (directory/"artifacts/final.mp4").write_bytes(b"video")
    async def probe(*args):return {"duration":10}
    async def command(*args,**kwargs):return 0,"界面显示成功"
    monkeypatch.setattr(module._Run,"probe",probe)
    monkeypatch.setattr(module._Run,"command",command)
    result=execute(case,"publish",action="publish")
    assert result["status"]=="blocked"
    receipt=json.loads((directory/"artifacts/publication-receipt.json").read_text(encoding="utf-8"))
    assert receipt["outcome"]=="submitted" and receipt["verified"] is False


def test_cancel_kills_owned_process_and_propagates(case):
    async def run():
        runner=module._Run(WorkflowRunner(case[0]),case[2],"brief",{}, {},case[1],lambda _:None)
        task=asyncio.create_task(runner.command([module.sys.executable,"-c","import time; time.sleep(60)"],label="cancel test"))
        await asyncio.sleep(.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        assert "仅停止本次子进程树" in runner.log_path.read_text(encoding="utf-8")
    asyncio.run(run())


@pytest.mark.skipif(os.environ.get("EASEL_RUN_RENDER_SMOKE")!="1",reason="Opt-in actual installed Remotion and ffmpeg render")
def test_real_sdk_template_render_smoke(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    base=root/"outputs/视频工作流"
    base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runner-smoke-",dir=base) as temp:
        directory=Path(temp)
        source=directory/"source.mp4"
        subprocess.run(["ffmpeg","-y","-v","error","-f","lavfi","-i","testsrc2=size=640x360:rate=30:duration=4","-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=4","-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(source)],check=True)
        subtitle=directory/"source.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n真实时间戳字幕\n\n2\n00:00:02,000 --> 00:00:04,000\n固定流程可以复现\n",encoding="utf-8")
        project={"id":directory.name,"kind":"video","title":"流程渲染验证","content_version":1,"settings":{"template":"documentary"},"media":{"source_path":str(source),"transcript_path":str(subtitle)}}
        async def model(*args,**kwargs):return {"background":"#152020","accent":"#D4AF72","textColor":"#FFFFFF","subtitleSize":54,"cardPosition":"left","titleCase":"bold"}
        monkeypatch.setattr(module,"generate",model)
        runner=WorkflowRunner(root)
        async def run():
            for node,options in [("source",{}),("transcript",{}),("storyboard",{"timeline":{"scenes":[{"start":0,"end":4,"title":"工作流","purpose":"真实渲染检查","card":"先固定执行步骤"}]}}),("build",{}),("review",{}),("deliver",{})]:
                result=await runner.execute(project,node,options,{"content":"暖灰、清晰大字幕，保留原片。"},directory,lambda text:None)
                assert result["status"] in {"completed","awaiting_review"},f"{node}: {result['message']}"
                project["media"].update(result.get("media") or {})
            assert (directory/"artifacts/final.mp4").stat().st_size>10000
            assert json.loads((directory/"artifacts/delivery-report.json").read_text(encoding="utf-8"))["decode"]=="passed"
        asyncio.run(run())
