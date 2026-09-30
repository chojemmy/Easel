"""Read-only research through the existing local OpenClaw web tools.

The gateway owns provider credentials, DNS/redirect SSRF checks and tool policy.
This adapter never starts an agent, accepts shell commands or exposes a generic
tool endpoint. All external page text remains untrusted source material.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import parse_qsl, urlsplit
import uuid

import httpx

from .content_workflow import safe_error
from .openclaw_workspace import config_path


class WorkflowResearchError(RuntimeError):
    """Search/fetch failed; callers must not invent research results."""


class WorkflowResearchConfigError(ValueError):
    """The configured local gateway cannot be used by this adapter."""


@dataclass(frozen=True)
class _Gateway:
    endpoint: str
    secret: str = field(repr=False)


def _secret(value, seen=None) -> str:
    visited = set() if seen is None else set(seen)
    if isinstance(value, dict):
        if value.get("source") != "env":
            raise WorkflowResearchConfigError("网关密钥使用了未支持的引用，请配置本机网关环境变量。")
        name = value.get("id")
    elif isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        if not match:
            if "${" in value or any(char in value for char in "\r\n"):
                raise WorkflowResearchConfigError("网关认证配置无效。")
            return value.strip()
        name = match.group(1)
    elif value is None:
        return ""
    else:
        raise WorkflowResearchConfigError("网关认证配置格式无效。")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in visited:
        raise WorkflowResearchConfigError("网关环境密钥引用无效。")
    visited.add(name)
    return _secret(os.environ.get(name, ""), visited)


def _public_url(value: str) -> str:
    """Validate without rewriting a source URL or resolving DNS outside Gateway."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("请输入有效的公开网页 HTTP(S) 地址。")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        parsed.port
    except ValueError:
        raise ValueError("网页地址格式无效。") from None
    if parsed.scheme not in {"https", "http"} or not hostname or parsed.username is not None or parsed.password is not None or "\\" in value:
        raise ValueError("只支持不含登录凭证的公开 HTTP(S) 网页。")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")):
        raise ValueError("工作流研究不能抓取本机或内网地址。")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname or all(part.isdigit() or part.startswith("0x") for part in hostname.split(".")):
            raise ValueError("工作流研究不能抓取本机或内网地址。") from None
    else:
        if not address.is_global:
            raise ValueError("工作流研究不能抓取本机或内网地址。")
    if any(re.search(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|^token$)", key) for key, _ in parse_qsl(parsed.query)):
        raise ValueError("请移除网页地址中的认证参数后再抓取。")
    return value


class WorkflowResearch:
    def __init__(self, config_file: Path | None = None, *, timeout: float = 45,
                 client_factory: Callable[..., httpx.AsyncClient] | None = None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("研究工具超时须在 0 到 120 秒之间。")
        self.config_file = Path(config_file) if config_file is not None else None
        self.timeout, self.client_factory = float(timeout), client_factory
        self.session_key = "agent:main:workflow-research-" + uuid.uuid4().hex

    def _gateway(self) -> _Gateway:
        path = self.config_file if self.config_file is not None else config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise WorkflowResearchConfigError("无法读取 Easel 的本机 OpenClaw 网关配置。") from None
        gateway = data.get("gateway", {}) if isinstance(data, dict) else None
        if not isinstance(gateway, dict) or not isinstance(gateway.get("auth", {}), dict):
            raise WorkflowResearchConfigError("本机 OpenClaw 网关配置格式无效。")
        port = gateway.get("port", 18789)
        if type(port) is not int or not 1 <= port <= 65535:
            raise WorkflowResearchConfigError("本机网关端口无效。")
        auth = gateway.get("auth", {})
        mode = auth.get("mode", "token")
        secret = ""
        if mode in {"token", "password", "trusted-proxy"}:
            key = "token" if mode == "token" else "password"
            secret = _secret(os.environ.get("OPENCLAW_GATEWAY_" + key.upper()) or auth.get(key))
            if not secret:
                raise WorkflowResearchConfigError("本机网关需要认证，请配置对应的网关 Token 或密码。")
        elif mode != "none":
            raise WorkflowResearchConfigError("当前网关认证方式不支持本机研究工具。")
        # Never accept a model-provided endpoint or forward a local operator key.
        return _Gateway(f"http://127.0.0.1:{port}/tools/invoke", secret)

    @staticmethod
    def _clean(text: str, secret: str) -> str:
        if secret:
            text = text.replace(secret, "[已隐藏凭证]")
        return safe_error(text, None)

    async def _request(self, gateway: _Gateway, tool: str, args: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if gateway.secret:
            headers["Authorization"] = "Bearer " + gateway.secret
        factory = self.client_factory or httpx.AsyncClient
        async with factory(timeout=httpx.Timeout(self.timeout, connect=min(10, self.timeout)),
                           follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", gateway.endpoint, headers=headers,
                                     json={"tool": tool, "args": args, "sessionKey": self.session_key}) as response:
                if response.status_code != 200:
                    labels = {401: "网关认证失败", 403: "网关策略禁止此研究工具", 404: "网关未提供此研究工具或未允许调用",
                              429: "网关或搜索服务限流，请稍后重试"}
                    raise WorkflowResearchError(f"{labels.get(response.status_code, '研究工具请求失败')}（HTTP {response.status_code}）。")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise WorkflowResearchError("研究工具返回内容过大，请缩小检索范围。")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise WorkflowResearchError("网关研究工具未返回有效 JSON。") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
            raise WorkflowResearchError("网关研究工具未返回成功结果。")
        return payload["result"]

    def _normalize(self, result: dict, gateway: _Gateway, *, tool: str, limit: int) -> dict:
        texts, documents = [], []
        details = result.get("details")
        if isinstance(details, dict):
            documents.append(details)
        content = result.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                    try:
                        parsed = json.loads(block["text"])
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        documents.append(parsed)
        if result.get("isError") or any(doc.get("error") or doc.get("kind") == "error" for doc in documents):
            diagnostic = next((str(doc.get("message") or doc.get("error")) for doc in documents if doc.get("message") or doc.get("error")), "研究工具报告错误，请检查网关搜索服务配置。")
            raise WorkflowResearchError(self._clean(diagnostic, gateway.secret)[:600])
        if not texts and documents:
            texts = [json.dumps(documents[0], ensure_ascii=False)]
        if not texts:
            raise WorkflowResearchError("研究工具没有返回可读取的文本。")
        sources, seen = [], set()
        def add_source(value, title=""):
            if not isinstance(value, str) or value in seen:
                return
            try:
                _public_url(value)
            except ValueError:
                return
            # A credential-bearing URL is omitted, never rewritten into a fake source.
            if self._clean(value, gateway.secret) != value:
                return
            seen.add(value)
            sources.append({"url": value, "title": self._clean(str(title or ""), gateway.secret)[:400]})
        for doc in documents:
            for key in ("results", "citations", "sources"):
                values = doc.get(key, [])
                if isinstance(values, list):
                    for item in values[:20]:
                        if isinstance(item, str):
                            add_source(item)
                        elif isinstance(item, dict):
                            add_source(item.get("url"), item.get("title", ""))
            for key in ("finalUrl", "url"):
                add_source(doc.get(key), doc.get("title", ""))
        cleaned = self._clean("\n\n".join(texts), gateway.secret)
        return {"tool": tool, "text": cleaned[:limit], "sources": sources[:10],
                "truncated": len(cleaned) > limit or len(sources) > 10,
                "untrusted": True}

    async def _invoke(self, tool: str, args: dict, limit: int) -> dict:
        allowed = {"web_search": {"query", "count"}, "web_fetch": {"url", "extractMode", "maxChars"}}
        if tool not in allowed or set(args) - allowed[tool]:
            raise ValueError("工作流研究只允许固定的网页搜索和抓取参数。")
        gateway = self._gateway()
        if any(isinstance(value, str) and self._clean(value, gateway.secret) != value for value in args.values()):
            raise ValueError("研究请求包含认证信息，请移除后再发送。")
        try:
            result = await asyncio.wait_for(self._request(gateway, tool, args), timeout=self.timeout)
        except (TimeoutError, httpx.TimeoutException):
            raise WorkflowResearchError("研究工具请求超时，请缩小范围或稍后重试。") from None
        except httpx.HTTPError:
            raise WorkflowResearchError("无法连接本机 OpenClaw 网关，请确认 Easel 网关正在运行。") from None
        return self._normalize(result, gateway, tool=tool, limit=limit)

    async def search(self, query: str, count: int = 5) -> dict:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 1000:
            raise ValueError("网页搜索关键词须为 2–1000 字。")
        if type(count) is not int or not 1 <= count <= 10:
            raise ValueError("搜索结果数量须为 1–10。")
        query = query.strip()
        return {**await self._invoke("web_search", {"query": query, "count": count}, 24000), "query": query}

    async def fetch(self, url: str, max_chars: int = 12000) -> dict:
        url = _public_url(url)
        if type(max_chars) is not int or not 1000 <= max_chars <= 30000:
            raise ValueError("网页摘录长度须为 1000–30000 字。")
        result = await self._invoke("web_fetch", {"url": url, "extractMode": "markdown", "maxChars": max_chars}, max_chars)
        return {**result, "url": url}
