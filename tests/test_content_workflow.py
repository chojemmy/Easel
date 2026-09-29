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
