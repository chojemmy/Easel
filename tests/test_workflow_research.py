"""No live Gateway, model or search provider is called by these tests."""
import asyncio
import json

import httpx
import pytest

from easel.workflow_research import WorkflowResearch, WorkflowResearchConfigError, WorkflowResearchError


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)
    path = tmp_path / "openclaw.json"
    path.write_text(json.dumps({"gateway": {"port": 18789, "auth": {"mode": "none"}}}), encoding="utf-8")
    return path


def client(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def response(data, **kwargs):
    return httpx.Response(200, json={"ok": True, "result": {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "details": data, **kwargs}})


def test_search_uses_only_local_fixed_tool_and_retains_actual_source_urls(config):
    seen = []
    actual_url = "https://docs.example.com/guide?language=zh#section-2"
    def handler(request):
        seen.append(request)
        return response({"kind": "results", "results": [{"title": "原始来源", "url": actual_url, "snippet": "网页片段"}]})
    research = WorkflowResearch(config, client_factory=client(handler))
    result = asyncio.run(research.search("最新文档", count=3))
    assert str(seen[0].url) == "http://127.0.0.1:18789/tools/invoke"
    payload = json.loads(seen[0].content)
    assert payload == {"tool": "web_search", "args": {"query": "最新文档", "count": 3}, "sessionKey": research.session_key}
    assert research.session_key.startswith("agent:main:workflow-research-")
    assert "authorization" not in seen[0].headers
    assert result["sources"] == [{"title": "原始来源", "url": actual_url}]
    assert result["untrusted"] is True


@pytest.mark.parametrize("mode",["token", "password", "trusted-proxy"])
def test_gateway_auth_is_loopback_only_and_never_returned(config, monkeypatch, mode):
    key = "token" if mode == "token" else "password"
    config.write_text(json.dumps({"gateway": {"port": 20000, "remote": {"url": "https://unexpected.example"},
        "auth": {"mode": mode, key: "${TEST_RESEARCH_AUTH}"}}}), encoding="utf-8")
    monkeypatch.setenv("TEST_RESEARCH_AUTH", "private-local-operator-credential")
    def handler(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 20000
        assert request.headers["Authorization"] == "Bearer private-local-operator-credential"
        assert not any(name.startswith("x-forwarded") for name in request.headers)
        return response({"content": "文字 private-local-operator-credential", "citations": [{"url": "https://example.com/article"}]})
    research = WorkflowResearch(config, client_factory=client(handler))
    result = asyncio.run(research.search("资料查询"))
    assert "private-local-operator-credential" not in json.dumps(result)
    assert "private-local-operator-credential" not in repr(research._gateway())


def test_fetch_uses_gateway_guarded_fetch_and_caps_display_text(config):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tool"] == "web_fetch"
        assert payload["args"] == {"url": "https://example.com/start", "extractMode": "markdown", "maxChars": 1000}
        return response({"url": "https://example.com/start", "finalUrl": "https://example.com/final?lang=en", "title": "真实页面", "text": "A" * 2000})
    result = asyncio.run(WorkflowResearch(config, client_factory=client(handler)).fetch("https://example.com/start", max_chars=1000))
    assert result["url"] == "https://example.com/start"
    assert result["sources"][0]["url"] == "https://example.com/final?lang=en"
    assert len(result["text"]) == 1000 and result["truncated"] is True


@pytest.mark.parametrize("url",[
    "file:///D:/private.txt", "http://localhost:18789/tools/invoke", "https://127.0.0.1/", "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://2130706433/", "https://host.internal/a",
    "https://user:secret@example.com/", "https://example.com/?access_token=secret",
])
def test_fetch_rejects_private_or_credential_urls_before_network(config, url):
    def unexpected(_):
        pytest.fail("Unsafe URL must not reach Gateway")
    with pytest.raises(ValueError):
        asyncio.run(WorkflowResearch(config, client_factory=client(unexpected)).fetch(url))


@pytest.mark.parametrize("status",[301, 401, 403, 404, 429, 500])
def test_http_failure_does_not_follow_redirect_or_expose_raw_body(config, status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"location": "https://untrusted.example"}, text="SECRET_RAW_SERVER_DIAGNOSTIC")
    with pytest.raises(WorkflowResearchError) as error:
        asyncio.run(WorkflowResearch(config, client_factory=client(handler)).search("查询资料"))
    assert len(seen) == 1
    assert str(status) in str(error.value)
    assert "SECRET_RAW_SERVER_DIAGNOSTIC" not in str(error.value)


def test_provider_missing_key_or_error_is_not_successful_research(config):
    def handler(_):
        return response({"kind": "error", "error": "missing_api_key", "message": "请配置搜索服务 api_key=private-secret"})
    with pytest.raises(WorkflowResearchError) as error:
        asyncio.run(WorkflowResearch(config, client_factory=client(handler)).search("最新信息"))
    assert "请配置搜索服务" in str(error.value)
    assert "private-secret" not in str(error.value)


def test_response_size_limit_blocks_oversized_gateway_data(config):
    def handler(_):
        return httpx.Response(200, content=b"A" * 1_000_001)
    with pytest.raises(WorkflowResearchError, match="过大"):
        asyncio.run(WorkflowResearch(config, client_factory=client(handler)).search("资料查询"))


def test_only_web_tools_can_be_invoked(config):
    research = WorkflowResearch(config)
    with pytest.raises(ValueError):
        asyncio.run(research._invoke("exec", {"command": "anything"}, 1000))
    with pytest.raises(ValueError):
        asyncio.run(research._invoke("web_search", {"query": "资料", "action": "publish"}, 1000))


def test_cancellation_closes_inflight_request_without_retry(config):
    entered = asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()
    async def run():
        task = asyncio.create_task(WorkflowResearch(config, client_factory=client(handler)).search("资料查询"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(calls) == 1


def test_timeout_returns_no_fabricated_sources(config):
    async def handler(request):
        await asyncio.Event().wait()
    with pytest.raises(WorkflowResearchError, match="超时"):
        asyncio.run(WorkflowResearch(config, timeout=.02, client_factory=client(handler)).search("资料查询"))


def test_missing_gateway_secret_fails_before_network(config):
    config.write_text('{"gateway":{"auth":{"mode":"token"}}}', encoding="utf-8")
    with pytest.raises(WorkflowResearchConfigError, match="需要认证"):
        asyncio.run(WorkflowResearch(config).search("资料查询"))


def test_credentials_in_search_query_are_rejected_before_network(config):
    with pytest.raises(ValueError, match="认证信息"):
        asyncio.run(WorkflowResearch(config).search("搜一下 api_key=not-for-provider"))
