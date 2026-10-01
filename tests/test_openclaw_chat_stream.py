"""Exercise the actual HTTP adapter without importing production web.app."""
import ast
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from easel.openclaw_chat_stream import generation_limit


def test_native_budget_uses_current_model_ceiling_and_explicit_node_selection(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"agents": {"defaults": {"model": {"primary": "workbuddy/dsv4"}}},
        "models": {"providers": {"workbuddy": {"apiKey": "DO NOT EXPOSE", "models": [{"id": "dsv4", "maxTokens": 65536}]}}}}))
    assert generation_limit(config_path=config) == 65536
    assert generation_limit("maximum", config_path=config) == 65536
    assert generation_limit("large", config_path=config) == 32768
    assert generation_limit("standard", config_path=config) == 8192
    assert generation_limit(max_tokens=8192, config_path=config) == 8192
    for value in (True, 65537, 0):
        with pytest.raises(ValueError):
            generation_limit(max_tokens=value, config_path=config)


def http_adapter():
    source = Path(__file__).resolve().parents[1] / "web/app.py"
    module = ast.parse(source.read_text(encoding="utf-8-sig"))
    handler = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_chat_stream")
    supervisor = next(n for n in handler.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "supervisor")
    transport = next(n for n in supervisor.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_gateway_turn")
    return source, transport


@pytest.mark.parametrize("finish,saw_done,error", [("stop", True, False), ("length", True, False),
    ("stop", False, False), (None, True, False), ("stop", True, True)])
def test_http_completion_does_not_hide_length_or_broken_stream(monkeypatch, finish, saw_done, error):
    captured, emitted, statuses = [], [], []
    state = {"stop_reason": None, "error": False, "token_chars": 0, "thinking_chars": 0,
             "last_ev": None, "saw_message_end": False, "run_id": None}
    chunks = [{"id": "this-run", "choices": [{"delta": {"content": "The"}, "finish_reason": None}]},
              {"id": "this-run", "choices": [{"delta": {}, "finish_reason": finish}]}]
    if error:
        chunks.append({"error": {"message": "provider overloaded"}})
    class Response:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            for chunk in chunks:
                yield "data: " + json.dumps(chunk)
            if saw_done:
                yield "data: [DONE]"
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            captured.append(kwargs)
            return Response()
    class Activity:
        run_id = None
        def bind_run(self, run): self.run_id = run
    def status(kind, text):
        statuses.append((kind, text))
        if kind == "error": state["error"] = True
    fake_httpx = SimpleNamespace(AsyncClient=Client, Timeout=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    source, transport = http_adapter()
    namespace = {"req": SimpleNamespace(maxTokens=65536), "message": "讨论分镜", "sk": "workflow-test",
        "_openclaw_session_id": lambda key: "same-native-session", "tool_activity": Activity(),
        "TIMEOUT_CHAT": 900, "json": json, "asyncio": asyncio, "run_info": state,
        "to_client": status, "_emit": lambda *args: emitted.append(args), "q": asyncio.Queue(), "SENTINEL": object()}
    exec(compile(ast.Module(body=[transport], type_ignores=[]), str(source), "exec"), namespace)
    finished = []
    asyncio.run(namespace["_run_gateway_turn"](SimpleNamespace(finish=lambda: finished.append(True))))
    assert captured[0]["json"]["max_tokens"] == 65536
    assert captured[0]["headers"]["x-openclaw-session-id"] == "same-native-session"
    assert emitted == [("token", "The")] and state["token_chars"] == 3 and finished == [True]
    if error:
        assert state["stop_reason"] == "gateway_error" and statuses
    elif not saw_done or not finish:
        assert state["stop_reason"] == "gateway_error" and statuses
    elif finish == "length":
        assert state["stop_reason"] == "length" and state["saw_message_end"] is False
    else:
        assert state["stop_reason"] == "stop" and state["last_ev"] == "assistant_message_end"
        assert state["saw_message_end"] is True and not statuses
