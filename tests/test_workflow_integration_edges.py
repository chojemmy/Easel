"""User-visible edit/retry semantics with deterministic, offline tool doubles."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.content_workflow import ContentWorkflowService, node_of
from easel.content_workflow_api import router
from easel.workflow_runner import WorkflowRunner, _Run, file_sha256, write_json


@pytest.fixture
def service(tmp_path):
    instance = ContentWorkflowService(tmp_path / "project", vault=tmp_path / "vault")
    instance.executor = WorkflowRunner(instance.root)
    return instance


def test_saved_script_feedback_changes_the_next_generated_manuscript(service, monkeypatch):
    seen = []

    async def model(prompt, *, system):
        seen.append(prompt + system)
        return "修改后的新稿件"

    monkeypatch.setattr("easel.workflow_runner.generate", model)

    async def scenario():
        project = service.create({"title": "记录意见后改稿", "manuscripts": [
            {"id": "old", "title": "原稿", "content": "原稿正文", "source_kind": "manual"},
        ]})
        service.feedback(project["id"], "script", "请把开场改成施工现场的具体例子。")
        await service.run(project["id"], "script", {})
        await service.tasks[project["id"]]
        current = service.get(project["id"])
        assert len(seen) == 1
        assert "施工现场的具体例子" in seen[0]
        manuscript = next(item for item in current["manuscripts"] if item["id"] == current["primary_manuscript_id"])
        assert manuscript["content"] == "修改后的新稿件"
        assert any(item["content"] == "原稿正文" for item in current["manuscripts"])

    asyncio.run(scenario())


def test_redelivery_renders_new_props_instead_of_reimporting_its_old_final(service, monkeypatch):
    rendered = []

    async def render(self, command, *args, **kwargs):
        assert command == "render"
        rendered.append(command)
        Path(args[1]).write_bytes(b"new-render-for-current-props")
        return 0, ""

    async def probe(self, path):
        return {"duration": 4, "width": 1920, "height": 1080, "fps": "30/1", "audio": True}

    async def command(self, args, **kwargs):
        if str(args[-1]).endswith("cover.jpg"):
            Path(args[-1]).write_bytes(b"cover")
        return 0, ""

    monkeypatch.setattr(_Run, "remotion", render)
    monkeypatch.setattr(_Run, "probe", probe)
    monkeypatch.setattr(_Run, "command", command)

    async def scenario():
        project = service.create({"title": "修改风格后交付"})
        directory = service.directory(project["id"])
        artifacts = directory / "artifacts"
        artifacts.mkdir()
        old_final = artifacts / "final.mp4"
        old_final.write_bytes(b"old-render")
        work = directory / "remotion"
        (work / "public").mkdir(parents=True)
        source = work / "public" / "source.mp4"
        source.write_bytes(b"registered-original-recording")
        template = service.root / "assets/workflow-template"
        shutil.copytree(Path(__file__).resolve().parents[1] / "assets/workflow-template", template)
        shutil.copytree(template / "src", work / "src")
        write_json(work / "template-ownership.json", {path.relative_to(work).as_posix(): file_sha256(path)
                                                    for path in (work / "src").rglob("*") if path.is_file()})
        (work / "props.json").write_text(json.dumps({
            "source": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "duration": 4, "width": 1920, "height": 1080, "fps": 30,
            "captions": [], "scenes": [], "visual": {"template": "editorial","subtitleSize":48,
                "background":"#E9E4DB","accent":"#768275","textColor":"#172120","cardPosition":"left","titleCase":"bold"},
        }), encoding="utf-8")
        project["media"]["final_path"] = str(old_final)
        from easel.workflow_render_settings import preferences_for
        props=json.loads((work/"props.json").read_text(encoding="utf-8"))
        snapshot=artifacts/"approved-props.json"
        write_json(snapshot,props)
        prefs=preferences_for(props,{})
        project['settings']['render_preferences']=prefs
        node_of(project, "review").update(status="completed",version=2,approved_version=2,
            render_receipt={"version":2,"preferences":prefs,"props_path":str(snapshot),
            "props_sha256":file_sha256(snapshot),"base_props_sha256":file_sha256(work/"props.json"),
            "source_sha256":props['source_sha256'],"duration_seconds":4,"music":None,"requests":[]})
        service.save(project)
        await service.run(project["id"], "deliver", {})
        await service.tasks[project["id"]]
        current = service.get(project["id"])
        assert node_of(current, "deliver")["status"] == "awaiting_review", node_of(current, "deliver")["message"]
        assert rendered == ["render"]
        assert Path(current['media']['final_path']).read_bytes() == b"new-render-for-current-props"
        assert old_final.read_bytes() == b"old-render"

    asyncio.run(scenario())


def test_matching_human_reconciliation_releases_runner_unknown_receipt_once(service, monkeypatch):
    submitted = []

    async def probe(self, path):
        return {"duration": 4, "width": 1920, "height": 1080, "fps": "30/1", "audio": True}

    async def command(self, args, **kwargs):
        if "--exec" in args:
            submitted.append(args)
            return 0, "草稿箱标题回读已确认"
        return 0, "预检通过"

    monkeypatch.setattr(_Run, "probe", probe)
    monkeypatch.setattr(_Run, "command", command)

    async def scenario():
        project = service.create({"title": "已核实未提交"})
        directory = service.directory(project["id"])
        artifacts = directory / "artifacts"
        artifacts.mkdir()
        (artifacts / "final.mp4").write_bytes(b"local-final")
        old_run_id = "run-uncertain-old"
        node_of(project, "deliver")["status"] = "completed"
        node_of(project, "publish").update(status="blocked", publication_uncertain=True,
            runs=[{"id": old_run_id, "action": "draft", "status": "blocked"}])
        receipt = {"workflow_id": project["id"], "run_id": old_run_id, "outcome": "unknown",
                   "verified": False, "action": "platform_draft", "platform": "weixin-channels",
                   "content_version": project["content_version"]}
        (artifacts / "publication-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        service.save(project)
        service.reconcile_not_submitted(project["id"], "已登录视频号助手确认没有新增这条草稿。")
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        current = service.get(project["id"])
        assert len(submitted) == 1
        assert node_of(current, "publish")["status"] == "completed"
        # The successful read-back receipt prevents a duplicate on another run.
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        assert len(submitted) == 1

    asyncio.run(scenario())


def test_publish_receipt_tracks_changed_video_bytes_without_requiring_title_edit(service, monkeypatch):
    submitted = []

    async def probe(self, path):
        return {"duration": 4, "width": 1920, "height": 1080, "fps": "30/1", "audio": True}

    async def command(self, args, **kwargs):
        if "--exec" in args:
            submitted.append(args)
            return 0, "草稿箱标题回读已确认"
        return 0, "预检通过"

    monkeypatch.setattr(_Run, "probe", probe)
    monkeypatch.setattr(_Run, "command", command)

    async def scenario():
        project = service.create({"title": "新风格生成了新视频"})
        artifacts = service.directory(project["id"]) / "artifacts"
        artifacts.mkdir()
        final = artifacts / "final.mp4"
        final.write_bytes(b"old-render")
        node_of(project, "deliver")["status"] = "completed"
        service.save(project)
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        assert len(submitted) == 1
        # Skill updates/rerenders can replace the artifact without changing the
        # project's title/content_version. A previous receipt is for old bytes.
        final.write_bytes(b"new-render-after-style-change")
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        assert len(submitted) == 2
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        assert len(submitted) == 2
        # Archival bookkeeping does not change the payload already on platform.
        service.patch(project["id"], {"settings": {"archive_folder": "3-输出/新归档目录"}})
        await service.run(project["id"], "publish", {"action": "draft"})
        await service.tasks[project["id"]]
        assert len(submitted) == 2

    asyncio.run(scenario())


def test_approval_requires_the_version_of_the_product_that_was_reviewed(service):
    project = service.create({"title": "审核具体版本"})
    node_of(project, "script").update(status="awaiting_review", version=2)
    service.save(project)
    with pytest.raises(ValueError):
        service.approve(project["id"], "script", None)
    assert node_of(service.get(project["id"]), "script")["status"] == "awaiting_review"
    confirmed = service.approve(project["id"], "script", 2)
    assert node_of(confirmed, "script")["approved_version"] == 2


def test_bad_personal_skill_does_not_hide_projects_or_start_a_partial_run(service):
    project = service.create({"title": "仍可打开的项目"})
    path = Path(service.skills.get("brief")["target_path"])
    path.parent.mkdir(parents=True)
    path.write_text("用户正在手动编辑，尚未补全 frontmatter", encoding="utf-8")
    app = FastAPI()
    app.include_router(router(service))
    with TestClient(app, raise_server_exceptions=False) as client:
        listing = client.get("/api/content-workflows")
        assert listing.status_code == 200
        assert listing.json()["projects"][0]["id"] == project["id"]
        detail = client.get(f"/api/content-workflows/{project['id']}")
        assert detail.status_code == 200
        response = client.post(f"/api/content-workflows/{project['id']}/nodes/brief/run", json={})
        assert response.status_code == 400
        assert node_of(service.get(project["id"]), "brief")["status"] == "idle"
        assert project["id"] not in service.tasks
