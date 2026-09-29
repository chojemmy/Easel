import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easel.content_workflow import ContentWorkflowService, WorkflowConflict, node_of, write_json
from easel.content_workflow_api import router


@pytest.fixture
def service(tmp_path):
    vault = tmp_path / "vault"
    (vault / "3-输出" / "草稿").mkdir(parents=True)
    return ContentWorkflowService(tmp_path / "easel", vault=vault)


def manuscript(text="一份稿件", mid="m1"):
    return {"id": mid, "title": "稿件", "content": text, "source_kind": "manual"}


def test_multiple_sources_and_primary_survive_restart(service):
    p = service.create({"title": "独立项目", "manuscripts": [manuscript(), manuscript("第二稿", "m2")], "primary_manuscript_id": "m2"})
    p2 = service.create({"title": "另一篇文章", "kind": "article"})
    restored = ContentWorkflowService(service.root, service.vault).get(p["id"])
    assert restored["primary_manuscript_id"] == "m2"
    assert len(restored["manuscripts"]) == 2
    assert restored["id"] != p2["id"]
    assert node_of(p2, "build")["status"] == "skipped"


def test_update_requires_current_version_and_invalidates_downstream(service):
    p = service.create({"title": "标题", "manuscripts": [manuscript()]})
    for n in p["nodes"]:
        n.update(status="completed", version=1, approved_version=1)
    service.save(p)
    updated = service.patch(p["id"], {"manuscripts": [manuscript("修改后的稿件")], "content_version": p["content_version"]})
    assert node_of(updated, "brief")["status"] == "completed"
    assert all(node_of(updated, n)["status"] == "stale" for n in ("script", "source", "build", "archive"))
    assert updated["manuscripts"][0]["version"] == 2
    with pytest.raises(WorkflowConflict):
        service.patch(p["id"], {"title": "旧窗口", "content_version": p["content_version"]})
    unchanged = service.patch(p["id"], {"manuscripts": updated["manuscripts"]})
    assert unchanged["content_version"] == updated["content_version"]


def test_publish_copy_does_not_invalidate_render(service):
    p = service.create({"title": "标题"})
    for n in p["nodes"]:
        n["status"] = "completed"
    service.save(p)
    updated = service.patch(p["id"], {"settings": {"publish_title": "发布用标题"}})
    assert node_of(updated, "deliver")["status"] == "completed"
    assert node_of(updated, "publish")["status"] == "stale"


@pytest.mark.parametrize("settings", [
    {"visual_style": "米白编辑排版"},
    {"output_ratio": "9:16"},
    {"template": "editorial"},
    {"subtitle_style": "字号更大、清晰留白"},
    {"visual_parameters": {"background": "#FAF6EC", "accent": "#B64B32", "textColor": "#202020",
                           "subtitleSize": 56, "cardPosition": "right", "titleCase": "bold"}},
])
def test_visual_changes_preserve_confirmed_storyboard_and_require_new_render_review(service, settings):
    project = service.create({"title": "已确认分镜后换视觉"})
    for node in project["nodes"]:
        node.update(status="completed", version=3, approved_version=3)
    storyboard = node_of(project, "storyboard")
    storyboard["artifacts"] = [{"name": "已确认分镜", "path": "artifacts/storyboard.json"}]
    storyboard["approved_at"] = "2026-09-29T00:00:00+00:00"
    service.save(project)

    updated = service.patch(project["id"], {"settings": settings, "content_version": project["content_version"]})

    assert node_of(updated, "storyboard") == storyboard
    assert all(node_of(updated, node)["status"] == "completed"
               for node in ("brief", "script", "source", "transcript"))
    for name in ("build", "review", "deliver", "publish", "archive"):
        assert node_of(updated, name)["status"] == "stale"
        assert "approved_version" not in node_of(updated, name)
    # The user can rebuild immediately without redoing the approved timeline.
    service.prerequisites(updated, "build")
    with pytest.raises(WorkflowConflict):
        service.prerequisites(updated, "deliver")


@pytest.mark.parametrize("parameter_input", ["omitted", "echo_previous", "explicit_new"])
def test_style_edit_replaces_stale_visual_parameters_and_keeps_explicit_new_choices(service, parameter_input):
    previous = {"background": "#182020", "accent": "#D1B479", "textColor": "#F5F1E8",
                "subtitleSize": 48, "cardPosition": "left", "titleCase": "bold"}
    replacement = {**previous, "background": "#FAF6EC", "subtitleSize": 60}
    project = service.create({"title": "已有显式参数后修改风格", "settings": {"visual_parameters": previous}})
    settings = {"visual_style": "米白杂志风", "subtitle_style": "大字幕"}
    if parameter_input == "echo_previous":
        settings["visual_parameters"] = previous
    elif parameter_input == "explicit_new":
        settings["visual_parameters"] = replacement

    updated = service.patch(project["id"], {"settings": settings})

    assert updated["settings"]["visual_style"] == "米白杂志风"
    if parameter_input == "explicit_new":
        assert updated["settings"]["visual_parameters"] == replacement
    else:
        # Next build must interpret the newly requested style instead of using
        # an old explicit parameter block silently carried forward by the UI.
        assert "visual_parameters" not in updated["settings"]


@pytest.mark.parametrize("subtitle_selection", ["omitted", "same_old_path", "new_path"])
def test_replacing_recording_clears_old_transcript_binding_but_preserves_originals(service, tmp_path, subtitle_selection):
    original = tmp_path / "original.mp4"
    original.write_bytes(b"original-recording")
    old_subtitle, new_subtitle = tmp_path / "original.srt", tmp_path / "replacement.srt"
    old_subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n原片字幕\n", encoding="utf-8")
    new_subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n新片字幕\n", encoding="utf-8")
    project = service.create({"title": "替换录制"})
    project["media"] = {"source_path": str(original), "transcript_path": str(old_subtitle),
                        "generated_transcript_path": "artifacts/transcript.json", "transcript_source_sha256": "old-source-hash"}
    for node in project["nodes"]:
        node.update(status="completed", version=1, approved_version=1)
    service.save(project)
    incoming = {"source_path": str(tmp_path / "replacement.mp4")}
    if subtitle_selection != "omitted":
        incoming["transcript_path"] = str(new_subtitle if subtitle_selection == "new_path" else old_subtitle)

    updated = service.patch(project["id"], {"media": incoming})

    if subtitle_selection == "new_path":
        assert updated["media"]["transcript_path"] == str(new_subtitle)
    else:
        assert "transcript_path" not in updated["media"]
    assert "generated_transcript_path" not in updated["media"]
    assert "transcript_source_sha256" not in updated["media"]
    assert node_of(updated, "script")["status"] == "completed"
    assert all(node_of(updated, name)["status"] == "stale" for name in ("source", "transcript", "build", "deliver"))
    assert original.read_bytes() == b"original-recording"
    assert "原片字幕" in old_subtitle.read_text(encoding="utf-8")


def test_review_feedback_is_delivered_to_selected_upstream_node_once(service):
    seen = []

    class Executor:
        async def execute(self, project, node, options, *args):
            seen.append((node, options.get("feedback")))
            return {"message": "已按本次意见生成修订稿"}

    async def scenario():
        service.executor = Executor()
        project = service.create({"title": "从样片反馈到写稿"})
        for node in project["nodes"]:
            node.update(status="completed", version=1, approved_version=1)
        service.save(project)
        feedback = "请把第二段的抽象结论改为一个施工现场例子。"
        updated = service.feedback(project["id"], "review", feedback, "script")
        assert node_of(updated, "brief")["status"] == "completed"
        assert node_of(updated, "script")["status"] == "stale"
        assert node_of(updated, "review")["status"] == "stale"
        await service.run(project["id"], "script", {})
        await service.tasks[project["id"]]
        assert seen == [("script", feedback)]
        await service.run(project["id"], "script", {})
        await service.tasks[project["id"]]
        assert seen == [("script", feedback), ("script", None)]

    asyncio.run(scenario())


def test_note_import_stays_in_vault_and_keeps_original(service, tmp_path):
    note = service.vault / "我的文章.md"
    note.write_text("原始资料", encoding="utf-8")
    p = service.create({"title": "引用笔记"})
    result = service.import_note(p["id"], {"source_path": "我的文章.md"})
    assert result["manuscripts"][0]["content"] == "原始资料"
    assert service.obsidian_search("文章")[0]["path"] == "我的文章.md"
    outside = tmp_path / "secret.md"
    outside.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError):
        service.import_note(p["id"], {"source_path": str(outside)})
    assert note.read_text(encoding="utf-8") == "原始资料"


def test_id_and_primary_validation(service):
    with pytest.raises(ValueError):
        service.get("../other")
    p = service.create({"title": "标题"})
    with pytest.raises(ValueError):
        service.patch(p["id"], {"manuscripts": [manuscript(), manuscript()]})
    with pytest.raises(ValueError):
        service.patch(p["id"], {"primary_manuscript_id": "not-present"})


def test_run_progress_approval_and_latest_skill_snapshot(service):
    async def scenario():
        class Executor:
            async def execute(self, p, node, options, skill, directory, progress):
                progress("已完成实际工作")
                assert "SKILL" not in skill["version"]
                path = directory / "brief.md"
                path.write_text("验收依据", encoding="utf-8")
                return {"artifacts": [{"name": "简报", "path": str(path), "kind": "text"}]}
        service.executor = Executor()
        p = service.create({"title": "运行"})
        result = await service.run(p["id"], "brief", {})
        assert node_of(result, "brief")["status"] == "running"
        task = service.tasks[p["id"]]
        with pytest.raises(WorkflowConflict):
            service.patch(p["id"], {"title": "不能运行时覆盖"})
        await task
        result = service.get(p["id"])
        assert node_of(result, "brief")["status"] == "awaiting_review"
        with pytest.raises(WorkflowConflict):
            service.approve(p["id"], "brief", 999)
        approved = service.approve(p["id"], "brief", 1)
        assert node_of(approved, "brief")["status"] == "completed"
    asyncio.run(scenario())


def test_failure_and_cancel_never_advance(service):
    async def scenario():
        class Executor:
            async def execute(self, *args):
                await asyncio.sleep(30)
                return {}
        service.executor = Executor()
        p = service.create({"title": "停止"})
        await service.run(p["id"], "brief", {})
        await asyncio.sleep(0)
        stopped = await service.stop(p["id"], "brief")
        assert node_of(stopped, "brief")["status"] == "blocked"
        assert p["id"] not in service.tasks
        class BrokenExecutor:
            async def execute(self, *args):
                raise ValueError("依赖文件缺失")
        service.executor = BrokenExecutor()
        await service.run(p["id"], "brief", {})
        await service.tasks[p["id"]]
        failed = node_of(service.get(p["id"]), "brief")
        assert failed["status"] == "failed"
        assert failed["message"] == "依赖文件缺失"
    asyncio.run(scenario())


def test_retry_preserves_old_failure_reason_without_persisting_credentials(service, monkeypatch):
    secret = "fixture-workflow-secret-123456789"
    monkeypatch.setenv("EASEL_WORKFLOW_KEY", secret)

    async def scenario():
        class Executor:
            attempts = 0

            async def execute(self, *args):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("upstream refused request; Authorization: Bearer " + secret)
                return {"message": "重试已生成新的简报，请审阅"}

        service.executor = Executor()
        project = service.create({"title": "保留可诊断的失败记录"})
        await service.run(project["id"], "brief", {})
        await service.tasks[project["id"]]
        failed_node = node_of(service.get(project["id"]), "brief")
        assert failed_node["status"] == "failed"
        failed_message = failed_node["message"]
        assert "upstream refused request" in failed_message
        assert secret not in failed_message

        await service.run(project["id"], "brief", {})
        await service.tasks[project["id"]]
        restored = ContentWorkflowService(service.root, service.vault).get(project["id"])
        node = node_of(restored, "brief")
        assert node["status"] == "awaiting_review"
        assert node["runs"][0]["status"] == "failed"
        assert node["runs"][0]["message"] == failed_message
        assert node["runs"][1]["message"] == "重试已生成新的简报，请审阅"
        assert secret not in json.dumps(restored)
        assert secret not in (service.directory(project["id"]) / "project.json").read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_restart_marks_interrupted_and_keeps_artifacts(service):
    p = service.create({"title": "中断恢复"})
    n = node_of(p, "build")
    n.update(status="running", runs=[{"id": "test", "status": "running"}], artifacts=[{"path": "x.tsx"}])
    service.save(p)
    restored = ContentWorkflowService(service.root, service.vault).get(p["id"])
    assert node_of(restored, "build")["status"] == "blocked"
    assert node_of(restored, "build")["artifacts"] == [{"path": "x.tsx"}]


def test_unknown_publication_cannot_retry(service):
    async def scenario():
        p = service.create({"title": "待核实"})
        node_of(p, "deliver")["status"] = "completed"
        node_of(p, "publish").update(status="blocked", publication_uncertain=True)
        service.save(p)
        with pytest.raises(WorkflowConflict, match="不能自动重发"):
            await service.run(p["id"], "publish", {"action": "publish", "confirm": True})
    asyncio.run(scenario())


def test_http_archive_unpublished_article_and_skill_export(service):
    app = FastAPI()
    app.include_router(router(service))
    with TestClient(app) as client:
        response = client.post("/api/content-workflows", json={"title": "可存档的文章", "kind": "article", "manuscripts": [manuscript()]})
        assert response.status_code == 200
        pid = response.json()["id"]
        base = f"/api/content-workflows/{pid}"
        preview = client.get(base + "/archive/preview")
        assert preview.status_code == 200
        no_confirm = client.post(base + "/archive", json={"expected_hash": preview.json()["hash"]})
        assert no_confirm.status_code == 400
        archived = client.post(base + "/archive", json={"confirm": True, "expected_hash": preview.json()["hash"]})
        assert archived.status_code == 200, archived.text
        assert node_of(archived.json()["project"], "archive")["status"] == "completed"
        assert Path(archived.json()["plan"]["note_path"]).is_file()
        proposal = client.post(base + "/nodes/script/learn/preview", json={"instruction": "每篇稿件开头用一个具体场景，不用空泛提问。"})
        assert proposal.status_code == 200, proposal.text
        assert "+" in proposal.json()["diff"]
        applied = client.post(base + "/nodes/script/learn/apply", json={"proposal_id": proposal.json()["proposal_id"]})
        assert applied.status_code == 200, applied.text
        assert applied.json()["personalized"]
        exported = client.get(base + "/skills/export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert len([n for n in archive.namelist() if n.endswith("SKILL.md")]) == 10
            assert any("具体场景" in archive.read(n).decode("utf-8") for n in archive.namelist() if n.endswith("SKILL.md"))
        assert client.post(base + "/archive", json={"confirm": True}, headers={"Origin": "https://untrusted.example"}).status_code == 403


def test_artifact_serving_rejects_traversal(service):
    app = FastAPI()
    app.include_router(router(service))
    p = service.create({"title": "产物路径"})
    directory = service.directory(p["id"])
    (directory / "artifacts").mkdir()
    (directory / "artifacts" / "中文.md").write_text("产物", encoding="utf-8")
    with TestClient(app) as client:
        base = f"/api/content-workflows/{p['id']}/artifacts"
        assert client.get(base, params={"path": "artifacts/中文.md"}).text == "产物"
        assert client.get(base, params={"path": "project.json"}).status_code == 400
        assert client.get(base, params={"path": "../project.json"}).status_code == 400
