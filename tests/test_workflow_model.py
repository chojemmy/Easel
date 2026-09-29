"""HTTPX-mocked model tests: no real API calls, credentials or local config."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.workflow_model import WorkflowModel, WorkflowModelConfigError, WorkflowModelError


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("EASEL_WORKFLOW_MODEL", "EASEL_WORKFLOW_BASE_URL", "EASEL_WORKFLOW_KEY",
                 "EASEL_WORKFLOW_API", "EASEL_WORKFLOW_PROXY", "EASEL_PROXY", "MINIMAX_API_KEY",
                 "TEST_MODEL_KEY", "KEY_ALIAS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "minimax/MiniMax-current"}}},
        "models": {"providers": {"minimax": {
            "baseUrl": "https://api.minimax.cn/anthropic", "api": "anthropic-messages",
            "apiKey": "${TEST_MODEL_KEY}", "models": [{"id": "MiniMax-fallback"}],
        }}},
    }), encoding="utf-8")
    monkeypatch.setenv("TEST_MODEL_KEY", "test-credential-not-real")
    return path


def factory(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, **kwargs)


def run(model, **kwargs):
    return asyncio.run(model.generate("写一个短视频脚本", "仅返回稿件", **kwargs))


def test_current_model_direct_anthropic_text_only_request(config):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"content": [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": '{"script":"可用稿件"}'},
        ], "stop_reason": "end_turn"})

    model = WorkflowModel(config, client_factory=factory(handler))
    assert run(model) == '{"script":"可用稿件"}'
    assert str(seen[0].url) == "https://api.minimax.cn/anthropic/v1/messages"
    payload = json.loads(seen[0].content)
    assert payload["model"] == "MiniMax-current"
    assert payload["system"] == "仅返回稿件"
    assert payload["messages"] == [{"role": "user", "content": "写一个短视频脚本"}]
    assert payload["stream"] is False
    assert not {"tools", "tool_choice", "functions"}.intersection(payload)
    assert "test-credential-not-real" not in json.dumps(model.describe())
    assert "test-credential-not-real" not in repr(model._settings())


def test_env_overrides_allow_openai_compatible_provider(config, monkeypatch):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "strong-model")
    monkeypatch.setenv("EASEL_WORKFLOW_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("EASEL_WORKFLOW_KEY", "override-secret")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "成稿"}, "finish_reason": "stop"}]})

    assert run(WorkflowModel(config, client_factory=factory(handler))) == "成稿"
    assert str(seen[0].url) == "https://model.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer override-secret"
    assert json.loads(seen[0].content)["model"] == "strong-model"


def test_minimax_env_preferred_to_provider_alias_and_plaintext_key_never_used(config, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "preferred-env-secret")
    assert WorkflowModel(config)._settings().key == "preferred-env-secret"
    data = json.loads(config.read_text())
    data["models"]["providers"]["minimax"]["apiKey"] = "literal-secret-do-not-use"
    config.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.delenv("MINIMAX_API_KEY")
    with pytest.raises(WorkflowModelConfigError) as error:
        run(WorkflowModel(config))
    assert "literal-secret" not in str(error.value)


def test_changed_provider_origin_cannot_silently_reuse_minimax_key(config, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "original-provider-secret")
    monkeypatch.setenv("EASEL_WORKFLOW_BASE_URL", "https://different.example/v1")
    with pytest.raises(WorkflowModelConfigError, match="EASEL_WORKFLOW_KEY"):
        run(WorkflowModel(config))


def test_env_secret_ref_and_cycle_handling(config, monkeypatch):
    data = json.loads(config.read_text())
    data["models"]["providers"]["minimax"]["apiKey"] = {"source": "env", "provider": "default", "id": "KEY_ALIAS"}
    config.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("KEY_ALIAS", "${TEST_MODEL_KEY}")
    assert WorkflowModel(config)._settings().key == "test-credential-not-real"
    monkeypatch.setenv("TEST_MODEL_KEY", "${KEY_ALIAS}")
    with pytest.raises(WorkflowModelConfigError):
        run(WorkflowModel(config))


def test_config_not_required_when_all_explicit_overrides_present(tmp_path, monkeypatch):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "my-model")
    monkeypatch.setenv("EASEL_WORKFLOW_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("EASEL_WORKFLOW_KEY", "local-secret")
    assert WorkflowModel(tmp_path / "missing.json").describe()["model"] == "my-model"


@pytest.mark.parametrize("base", ["http://remote.example/v1", "https://secret@example.com/v1", "https://example.com/v1?key=secret", "file:///etc/config"])
def test_unsafe_base_is_rejected_without_network(config, monkeypatch, base):
    monkeypatch.setenv("EASEL_WORKFLOW_BASE_URL", base)
    with pytest.raises(WorkflowModelConfigError):
        run(WorkflowModel(config))


@pytest.mark.parametrize("response", [
    {"content": [], "stop_reason": "end_turn"},
    {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens"},
    {"content": [{"type": "tool_use", "name": "run_shell", "input": {"cmd": "danger"}}]},
    {"type": "error", "error": {"message": "test-credential-not-real"}},
    {"base_resp": {"status_code": 1008, "status_msg": "test-credential-not-real"}},
])
def test_failed_or_tool_results_never_become_success(config, response):
    with pytest.raises(WorkflowModelError) as error:
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, json=response))))
    assert "test-credential-not-real" not in str(error.value)


def test_http_error_body_and_transport_exception_do_not_expose_secret(config):
    with pytest.raises(WorkflowModelError) as error:
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(401, text="test-credential-not-real"))))
    assert "401" in str(error.value)
    assert "test-credential-not-real" not in str(error.value)

    def broken(request):
        raise httpx.ConnectError("test-credential-not-real", request=request)

    with pytest.raises(WorkflowModelError) as error:
        run(WorkflowModel(config, client_factory=factory(broken)))
    assert "test-credential-not-real" not in str(error.value)


def test_malformed_json_and_echoed_key_are_handled(config):
    with pytest.raises(WorkflowModelError):
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, text="not-json"))))
    answer = run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, json={
        "content": [{"type": "text", "text": "answer test-credential-not-real"}],
    }))))
    assert answer == "answer [redacted]"


def test_cancel_event_interrupts_inflight_http(config):
    async def scenario():
        started, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        model = WorkflowModel(config, client_factory=factory(handler))
        task = asyncio.create_task(model.generate("prompt", cancel=stop))
        await started.wait()
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_task_cancellation_closes_inflight_http(config):
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(WorkflowModel(config, client_factory=factory(handler)).generate("prompt"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_total_timeout_cancels_request_without_fallback(config):
    async def handler(request):
        await asyncio.sleep(10)

    with pytest.raises(WorkflowModelError, match="超时"):
        run(WorkflowModel(config, timeout=0.02, client_factory=factory(handler)))


def test_already_cancelled_does_not_read_configuration(tmp_path):
    async def scenario():
        stop = asyncio.Event()
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await WorkflowModel(tmp_path / "missing").generate("prompt", cancel=stop)

    asyncio.run(scenario())
