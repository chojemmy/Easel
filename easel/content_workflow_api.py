"""HTTP surface for the local content studio; no agent is needed to move state."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .content_workflow import ContentWorkflowService, WorkflowConflict, node_of, safe_error
from .workflow_archive import ArchiveConflict
from .workflow_skills import WorkflowSkillConflict


def router(service: ContentWorkflowService) -> APIRouter:
    api = APIRouter(prefix="/api/content-workflows", tags=["content-workflows"])

    def fail(exc: Exception):
        if isinstance(exc, (WorkflowConflict, WorkflowSkillConflict, ArchiveConflict)):
            raise HTTPException(409, str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(404, str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(400, str(exc)) from exc
        raise HTTPException(500, "本地文件操作失败，请查看服务日志。") from exc

    def mutation(request: Request):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise HTTPException(403, "仅允许本机工作台修改工作流")

    def project_view(p: dict) -> dict:
        directory = service.directory(p["id"]).resolve()
        for node in p["nodes"]:
            try:
                skill = service.skills.get(node["id"])
                node["current_skill_version"] = skill["version"]
                node["skill_updated"] = bool(node.get("skill_version") and node["skill_version"] != skill["version"])
            except (ValueError, OSError) as exc:
                # One malformed personal Skill must not hide all existing work.
                node["skill_error"] = safe_error(exc)
                node["skill_updated"] = True
            for artifact in node.get("artifacts", []):
                path = Path(artifact.get("path", ""))
                if not path.is_absolute():
                    path = directory / path
                path = path.resolve()
                if path.is_relative_to(directory) and path.is_file():
                    rel = path.relative_to(directory).as_posix()
                    artifact["url"] = f"/api/content-workflows/{p['id']}/artifacts?path={quote(rel, safe='')}"
        return p

    @api.get("")
    def listing():
        result = service.list()
        result["projects"] = [project_view(p) for p in result["projects"]]
        return result

    @api.post("")
    def create(request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            return project_view(service.create(body))
        except (ValueError, OSError) as exc:
            fail(exc)

    # Static paths precede /{project_id}.
    @api.get("/obsidian/search")
    def search(q: str = ""):
        try:
            return {"notes": service.obsidian_search(q)}
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}")
    def detail(project_id: str):
        try:
            return project_view(service.get(project_id))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.patch("/{project_id}")
    def patch(project_id: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            return project_view(service.patch(project_id, body))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/manuscripts/import")
    def import_note(project_id: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            return project_view(service.import_note(project_id, body))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/run")
    async def run(project_id: str, node: str, request: Request, body: dict = Body(default={})):
        mutation(request)
        try:
            return project_view(await service.run(project_id, node, body))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/stop")
    async def stop(project_id: str, node: str, request: Request):
        mutation(request)
        try:
            return project_view(await service.stop(project_id, node))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/approve")
    def approve(project_id: str, node: str, request: Request, body: dict = Body(default={})):
        mutation(request)
        try:
            return project_view(service.approve(project_id, node, body.get("version")))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/feedback")
    def feedback(project_id: str, node: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            return project_view(service.feedback(project_id, node, str(body.get("text", "")), body.get("target_node")))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/publish/reconcile")
    def reconcile(project_id: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            if body.get("confirm") is not True or body.get("outcome") != "not_submitted":
                raise ValueError("需要你先在平台核实未提交，再明确解除重试限制")
            return project_view(service.reconcile_not_submitted(project_id, body.get("note", "")))
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}/nodes/{node}/skills")
    def skill(project_id: str, node: str):
        try:
            node_of(service.get(project_id), node)
            result = service.skills.get(node)
            result["history"] = service.skills.history(node)
            return result
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/learn/preview")
    def learn_preview(project_id: str, node: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            p = service.get(project_id)
            n = node_of(p, node)
            current = service.skills.get(node)
            if body.get("expected_version") not in (None, current["version"]):
                raise WorkflowConflict("节点标准已变，请刷新后重新提出经验。")
            return service.skills.propose(node, str(body.get("instruction", "")),
                context={"node_run_id": n["runs"][-1]["id"] if n["runs"] else ""})
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/nodes/{node}/learn/apply")
    def learn_apply(project_id: str, node: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            # Hold project lock across apply and invalidation so a run cannot slip in.
            with service._lock:
                service._idle(service.get(project_id))
                result = service.skills.apply(node, str(body.get("proposal_id", "")))
                service.skill_applied(project_id, node)
                return result
        except (ValueError, OSError) as exc:
            fail(exc)

    def zip_response(project_id: str, nodes=None):
        service.get(project_id)
        return Response(service.skills.export(nodes), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="easel-node-skills-{nodes[0] if nodes else "all"}.zip"'})

    @api.get("/{project_id}/skills/export")
    def export_all(project_id: str):
        try:
            return zip_response(project_id)
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}/nodes/{node}/skills/export")
    def export_node(project_id: str, node: str):
        try:
            return zip_response(project_id, [node])
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}/archive/preview")
    def archive_preview(project_id: str):
        try:
            return service.archive_preview(project_id)
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.post("/{project_id}/archive")
    def archive(project_id: str, request: Request, body: dict = Body(...)):
        mutation(request)
        try:
            if body.get("confirm") is not True or not body.get("expected_hash"):
                raise ValueError("请先查看归档预览并确认")
            result = service.archive_apply(project_id, str(body["expected_hash"]))
            result["project"] = project_view(result["project"])
            return result
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}/artifacts")
    def artifact(project_id: str, path: str):
        try:
            service.get(project_id)
            directory = service.directory(project_id).resolve()
            file = (directory / path).resolve()
            if not file.is_relative_to(directory) or not file.is_file() or file.name == "project.json":
                raise ValueError("无效的产物路径")
            media = {".mp4": "video/mp4", ".webm": "video/webm", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".json": "application/json"}
            return FileResponse(file, media_type=media.get(file.suffix.lower(), "text/plain; charset=utf-8"),
                                headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError) as exc:
            fail(exc)

    @api.get("/{project_id}/media")
    def media(project_id: str, kind: str = "source"):
        try:
            p = service.get(project_id)
            value = p["media"].get("source_path") if kind == "source" else p["settings"].get({"final": "final_path", "cover": "cover_path"}.get(kind, ""))
            if not value:
                raise FileNotFoundError("没有已登记的媒体文件")
            file = Path(value).resolve()
            if file.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".png", ".jpg", ".jpeg"}:
                raise ValueError("不是可预览的媒体文件")
            if not file.is_file():
                raise FileNotFoundError("媒体文件不存在")
            return FileResponse(file, headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError) as exc:
            fail(exc)

    return api
