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


@pytest.mark.parametrize("api,expected", [
    ("anthropic-messages", {"output_config": {"effort": "low"}}),
    ("openai-completions", {"reasoning_effort": "low"}),
])
def test_short_json_controls_flash_effort_without_changing_later_script(config, monkeypatch, api, expected):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/MiniMax-M3.1-Flash-Preview")
    monkeypatch.setenv("EASEL_WORKFLOW_API", api)
    seen, client_options = [], []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        })

    def client_factory(**kwargs):
        client_options.append(kwargs)
        return factory(handler)(**kwargs)

    model = WorkflowModel(config, client_factory=client_factory)
    assert run(model, task="short_json") == "{}"
    assert seen[0]["max_tokens"] == 2048
    assert all(seen[0].get(key) == value for key, value in expected.items())
    assert "thinking" not in seen[0]  # Flash rejects disabled thinking.
    assert client_options[0]["timeout"].read == 60
    assert run(model, task="script") == "{}"
    assert seen[1]["max_tokens"] == 131072
    assert not {"thinking", "output_config", "reasoning_effort"}.intersection(seen[1])
    assert client_options[1]["timeout"].read == 900


def test_explicit_budget_overrides_are_per_request(config, monkeypatch):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/MiniMax-M3.1-Flash-Preview")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "长稿"}]})

    model = WorkflowModel(config, client_factory=factory(handler))
    assert run(model, max_tokens=16384, effort="medium") == "长稿"
    assert seen[0]["max_tokens"] == 16384
    assert seen[0]["output_config"] == {"effort": "medium"}
    assert run(model) == "长稿"
    assert seen[1]["max_tokens"] == 131072 and "output_config" not in seen[1]


@pytest.mark.parametrize("name,thinking", [("MiniMax-M3", {"type": "disabled"}), ("MiniMax-M2.7", None), ("other-model", None)])
def test_short_json_only_disables_thinking_for_documented_m3(config, monkeypatch, name, thinking):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/" + name)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    assert run(WorkflowModel(config, client_factory=factory(handler)), task="short_json") == "{}"
    assert seen[0].get("thinking") == thinking
    assert "output_config" not in seen[0] and "reasoning_effort" not in seen[0]


@pytest.mark.parametrize("options", [
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 524289}, {"max_tokens": "2048"},
    {"effort": "none"}, {"effort": "low"}, {"task": "unknown"},
])
def test_unsupported_or_invalid_budget_is_rejected_before_http(config, options):
    def no_http(request):
        pytest.fail("invalid budgets must not reach the provider")

    with pytest.raises(ValueError):
        run(WorkflowModel(config, client_factory=factory(no_http)), **options)


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


def sse(event):
    return ("data: " + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)) + "\n\n").encode()


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            wire = sse(event)
            # Exercise UTF-8, JSON and SSE delimiters split at transport boundaries.
            for index in range(0, len(wire), 3):
                yield wire[index:index + 3]

    async def aclose(self):
        self.closed = True


def anthropic_text_events(chunks, reason="end_turn"):
    return [
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *[{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}} for chunk in chunks],
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": reason}},
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("api", ["anthropic-messages", "openai-completions"])
def test_real_streaming_delta_protocol_hides_reasoning_and_split_secret(config, monkeypatch, api):
    monkeypatch.setenv("EASEL_WORKFLOW_API", api)
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/MiniMax-M3.1-Flash-Preview")
    raw = "<ThInK>hidden chain</ThInK>答案：test-credential-not-real。2 < 3"
    if api == "anthropic-messages":
        events = [{"type": "content_block_start", "index": 9, "content_block": {"type": "thinking", "thinking": "never show this"}},
                  {"type": "content_block_delta", "index": 9, "delta": {"type": "thinking_delta", "thinking": "private reasoning"}}]
        events += anthropic_text_events(list(raw))
    else:
        events = [{"choices": [{"index": 0, "delta": {"reasoning_content": "never show this"}, "finish_reason": None}]}]
        events += [{"choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]} for chunk in raw]
        events += [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, "[DONE]"]
    stream = EventStream(events)
    sent, pieces = [], []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    answer = run(WorkflowModel(config, client_factory=factory(handler)), on_text=pieces.append, effort="low")
    assert answer == "答案：[redacted]。2 < 3"
    assert "".join(pieces) == answer
    assert len(pieces) > 1  # Callback is delta, not cumulative or a final replay.
    assert sent[0]["stream"] is True and sent[0]["max_tokens"] == 131072
    assert stream.closed


@pytest.mark.parametrize("events", [
    anthropic_text_events(["partial"], reason="max_tokens"),
    anthropic_text_events(["partial"])[:-1],
    anthropic_text_events(["<think>hidden chain"]),
    [{"type": "error", "error": {"message": "test-credential-not-real"}}],
    [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "secret_tool", "input": {"key": "test-credential-not-real"}}}],
    ["not-json"],
])
def test_incomplete_or_unsafe_stream_is_never_success(config, events):
    stream = EventStream(events)
    pieces = []
    with pytest.raises(WorkflowModelError) as error:
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, stream=stream))), on_text=pieces.append)
    assert "test-credential-not-real" not in str(error.value) + "".join(pieces)
    assert "hidden chain" not in "".join(pieces)
    assert stream.closed


@pytest.mark.parametrize("finish", ["length", "tool_calls", "content_filter"])
def test_openai_stream_rejects_truncation_and_tool_calls(config, monkeypatch, finish):
    monkeypatch.setenv("EASEL_WORKFLOW_API", "openai-completions")
    events = [{"choices": [{"delta": {"content": "unsafe", "tool_calls": [{"function": {"name": "secret"}}]} if finish == "tool_calls" else {"content": "partial"}, "finish_reason": finish}]}, "[DONE]"]
    pieces = []
    with pytest.raises(WorkflowModelError):
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, stream=EventStream(events)))), on_text=pieces.append)
    assert "unsafe" not in "".join(pieces)


@pytest.mark.parametrize("mode", ["cancel_event", "cancel_task", "timeout"])
def test_stream_emits_before_completion_and_closes_on_cancel_or_timeout(config, mode):
    async def scenario():
        received, stop = asyncio.Event(), asyncio.Event()
        pieces = []

        class WaitingStream(EventStream):
            async def __aiter__(self):
                for event in anthropic_text_events(["正文已开始。"])[0:3]:
                    yield sse(event)
                await received.wait()
                if mode == "cancel_event":
                    stop.set()
                await asyncio.sleep(10)

        def callback(text):
            pieces.append(text)
            received.set()

        stream = WaitingStream([])
        model = WorkflowModel(config, timeout=0.05 if mode == "timeout" else 1200,
                              client_factory=factory(lambda request: httpx.Response(200, stream=stream)))
        task = asyncio.create_task(model.generate("prompt", on_text=callback, cancel=stop))
        await asyncio.wait_for(received.wait(), 1)
        assert not task.done() and pieces
        if mode == "cancel_task":
            task.cancel()
        with pytest.raises(WorkflowModelError if mode == "timeout" else asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("profile,tokens,timeout", [("standard", 65536, 600), ("large", 131072, 900), ("maximum", 524288, 1200)])
def test_documented_m3_budget_profiles_override_short_json_when_selected(config, monkeypatch, profile, tokens, timeout):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/MiniMax-M3.1-Flash-Preview")
    sent, options = [], []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    def make_client(**kwargs):
        options.append(kwargs)
        return factory(handler)(**kwargs)

    assert run(WorkflowModel(config, client_factory=make_client), task="short_json", generation_budget=profile) == "{}"
    assert sent[0]["max_tokens"] == tokens
    assert sent[0]["output_config"] == {"effort": "low"}
    assert options[0]["timeout"].read == timeout


@pytest.mark.parametrize("model,budget", [("MiniMax-M3", 524289), ("MiniMax-M2.7", 204801), ("unknown", 65537)])
def test_model_specific_limit_rejected_before_network(config, monkeypatch, model, budget):
    monkeypatch.setenv("EASEL_WORKFLOW_MODEL", "minimax/" + model)
    with pytest.raises(ValueError):
        run(WorkflowModel(config, client_factory=factory(lambda request: pytest.fail("must not request"))), max_tokens=budget)


def test_stream_redacts_recognizable_credentials_not_loaded_from_environment(config):
    text = "Header: Bearer entirely-new-credential. Next: sk-not-the-configured-secret!"
    stream = EventStream(anthropic_text_events(list(text)))
    pieces = []
    result = run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, stream=stream))), on_text=pieces.append)
    assert "entirely-new-credential" not in result and "not-the-configured-secret" not in result
    assert "[redacted]" in result and "".join(pieces) == result


def test_stream_http_failure_does_not_read_or_replay_error_body(config):
    pieces = []
    with pytest.raises(WorkflowModelError) as error:
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(401, text="test-credential-not-real"))), on_text=pieces.append)
    assert "401" in str(error.value) and "test-credential-not-real" not in str(error.value)
    assert pieces == []


def test_openai_done_without_finish_reason_cannot_fake_success(config, monkeypatch):
    monkeypatch.setenv("EASEL_WORKFLOW_API", "openai-completions")
    events = [{"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}, "[DONE]"]
    with pytest.raises(WorkflowModelError):
        run(WorkflowModel(config, client_factory=factory(lambda request: httpx.Response(200, stream=EventStream(events)))), on_text=lambda text: None)
