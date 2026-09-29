"""Lifecycle/credential regressions for the content-workflow integration."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.content_workflow import ContentWorkflowService, WorkflowConflict, node_of


@pytest.fixture
def service(tmp_path):
    return ContentWorkflowService(tmp_path / "project", vault=tmp_path / "vault")


def test_immediate_stop_before_executor_starts_cannot_leave_project_running(service):
    async def scenario():
        class Executor:
            async def execute(self, *args):
                raise AssertionError("cancelled before scheduling; executor must not run")

        service.executor = Executor()
        project = service.create({"title": "立即停止"})
        await service.run(project["id"], "brief", {})
        # Deliberately no sleep(0): the created task has not entered _execute.
        stopped = await service.stop(project["id"], "brief")
        assert node_of(stopped, "brief")["status"] == "blocked"
        assert node_of(stopped, "brief")["runs"][-1]["status"] != "running"
        assert project["id"] not in service.tasks
        service.patch(project["id"], {"title": "可以继续修改"})

    asyncio.run(scenario())


def test_rejected_create_does_not_leave_a_phantom_project(service):
    with pytest.raises(ValueError):
        service.create({"title": "无效初始稿件", "manuscripts": "this should be a list"})
    assert service.list()["projects"] == []


def test_executor_errors_do_not_persist_environment_credentials(service, monkeypatch):
    key = "test-sensitive-workflow-key-123456789"
    monkeypatch.setenv("EASEL_WORKFLOW_KEY", key)

    async def scenario():
        class Executor:
            async def execute(self, *args):
                raise RuntimeError("upstream error: Authorization: Bearer " + key)

        service.executor = Executor()
        project = service.create({"title": "错误脱敏"})
        await service.run(project["id"], "brief", {})
        await service.tasks[project["id"]]
        result = service.get(project["id"])
        assert node_of(result, "brief")["status"] == "failed"
        assert key not in node_of(result, "brief")["message"]
        assert key not in (service.directory(project["id"]) / "project.json").read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_restart_during_confirmed_publish_requires_reconciliation_before_retry(service):
    project = service.create({"title": "发布回执中断"})
    node_of(project, "deliver")["status"] = "completed"
    node_of(project, "publish").update(status="running", runs=[{
        "id": "run-publish", "status": "running", "action": "publish", "content_version": project["content_version"],
    }])
    service.save(project)
    restored = ContentWorkflowService(service.root, service.vault)

    async def scenario():
        with pytest.raises(WorkflowConflict):
            await restored.run(project["id"], "publish", {"action": "publish", "confirm": True})

    asyncio.run(scenario())
